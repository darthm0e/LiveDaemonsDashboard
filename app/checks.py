"""Prüf-Logik für Dienste, VMs und Docker-Container.

Alle Netzwerk-Checks laufen asynchron, damit das Dashboard auch bei
vielen Zielen schnell antwortet. Ein Ziel gilt als 'up', wenn es
erreichbar ist – nicht jeder Check setzt einen laufenden Prozess voraus.
"""

import asyncio
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx


def _hostname(target: str) -> str:
    """Extrahiert eine anzeigbare Host/IP-Angabe aus einem Ziel."""
    if "://" in target:
        return urlparse(target).hostname or target
    if target.count(":") == 1 and not target.startswith("["):
        return target.split(":", 1)[0]
    return target


async def check_http(target, timeout, insecure, expect_status):
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=not insecure, timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.get(target)
        latency = (time.perf_counter() - start) * 1000
        ok = resp.status_code in expect_status if expect_status else resp.status_code < 500
        return ok, round(latency, 1), f"HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return False, None, "Timeout"
    except Exception as exc:  # noqa: BLE001
        return False, None, type(exc).__name__


async def check_tcp(host, port, timeout):
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        latency = (time.perf_counter() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True, round(latency, 1), f"Port {port} offen"
    except asyncio.TimeoutError:
        return False, None, "Timeout"
    except Exception as exc:  # noqa: BLE001
        return False, None, type(exc).__name__


async def check_ping(host, timeout):
    loop = asyncio.get_event_loop()

    def _ping():
        start = time.perf_counter()
        try:
            wait = str(max(1, int(round(timeout))))
            result = subprocess.run(
                ["ping", "-c", "1", "-W", wait, host],
                capture_output=True,
                text=True,
            )
            latency = (time.perf_counter() - start) * 1000
            if result.returncode == 0:
                return True, round(latency, 1), "Antwort erhalten"
            return False, None, "Keine Antwort"
        except FileNotFoundError:
            return False, None, "ping nicht installiert"
        except Exception as exc:  # noqa: BLE001
            return False, None, type(exc).__name__

    return await loop.run_in_executor(None, _ping)


async def run_check(chk: dict) -> dict:
    """Führt einen einzelnen konfigurierten Check aus und normalisiert das Ergebnis."""
    name = chk.get("name", "Unbenannt")
    ctype = (chk.get("type") or "ping").lower()
    target = str(chk.get("target", "")).strip()
    timeout = float(chk.get("timeout", 3))
    critical = bool(chk.get("critical", False))
    insecure = bool(chk.get("insecure", False))
    expect_status = set(chk.get("expect_status", []) or [])

    ok, latency, detail = False, None, "Kein Ziel"

    if target:
        if ctype == "http":
            ok, latency, detail = await check_http(target, timeout, insecure, expect_status)
        elif ctype == "tcp":
            host, _, port = target.rpartition(":")
            if host and port.isdigit():
                ok, latency, detail = await check_tcp(host, int(port), timeout)
            else:
                detail = "Ziel braucht host:port"
        elif ctype == "ping":
            ok, latency, detail = await check_ping(target, timeout)
        else:
            detail = f"Unbekannter Typ: {ctype}"

    return {
        "name": name,
        "type": ctype,
        "target": target,
        "host": _hostname(target),
        "ok": ok,
        "latency_ms": latency,
        "detail": detail,
        "critical": critical,
    }


_STARTED_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?")


def _uptime_seconds(started_at, running):
    """Sekunden seit Containerstart. None, wenn nicht laufend oder nicht ermittelbar.
    Docker liefert Nanosekunden + 'Z'; beides wird hier normalisiert."""
    if not running or not started_at or started_at.startswith("0001-01-01"):
        return None
    match = _STARTED_RE.match(started_at)
    if not match:
        return None
    base = match.group(1)
    frac = (match.group(2) or "")[:7]  # auf max. 6 Nachkommastellen kürzen
    try:
        started = datetime.fromisoformat(base + frac).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _port_map(attrs):
    """Liste lesbarer Port-Angaben, z.B. '8085→8080/tcp' (veröffentlicht)
    oder '5432/tcp' (nur exposed). Dedupliziert und sortiert."""
    ports = (attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    out = []
    for cport, bindings in sorted(ports.items()):
        if bindings:
            for hostport in sorted({b.get("HostPort") for b in bindings if b.get("HostPort")}):
                out.append(f"{hostport}\u2192{cport}")
        else:
            out.append(cport)
    return out


def get_docker_status(show_all=True, critical_names=None):
    """Liest Container vom Docker-Socket. Fehler werden nicht geworfen,
    sondern als 'error' zurückgegeben, damit das Dashboard trotzdem lädt."""
    critical_names = set(critical_names or [])
    try:
        import docker  # lokal importiert, damit App ohne SDK lauffähig bleibt
    except ImportError:
        return {"enabled": True, "containers": [], "error": "docker-SDK fehlt"}

    try:
        client = docker.from_env()
        containers = client.containers.list(all=show_all)
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "containers": [], "error": str(exc)}

    out = []
    for cont in containers:
        attrs = cont.attrs
        state = attrs.get("State", {}) or {}
        status = state.get("Status", "unknown")  # running / exited / paused ...
        health_obj = state.get("Health") or {}
        health = health_obj.get("Status")  # healthy / unhealthy / starting / None

        networks = (attrs.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
        ip = None
        for info in networks.values():
            if info.get("IPAddress"):
                ip = info["IPAddress"]
                break

        image = ""
        try:
            image = cont.image.tags[0] if cont.image.tags else (cont.image.short_id or "")
        except Exception:  # noqa: BLE001
            pass

        running = status == "running"
        ok = running and (health in (None, "healthy"))
        out.append(
            {
                "name": cont.name,
                "status": status,
                "health": health,
                "ip": ip,
                "image": image,
                "ports": _port_map(attrs),
                "uptime_seconds": _uptime_seconds(state.get("StartedAt"), running),
                "ok": ok,
                "critical": cont.name in critical_names,
            }
        )

    out.sort(key=lambda c: (c["ok"], c["name"].lower()))
    return {"enabled": True, "containers": out, "error": None}


# --------------------------------------------------------------------------
#  System-Vitaldaten via Glances REST-API
# --------------------------------------------------------------------------

def _gb(value):
    try:
        return round(float(value) / 1024 ** 3, 1)
    except (TypeError, ValueError):
        return None


def _fmt_uptime_str(val):
    """Glances liefert Uptime als String ('12 days, 3:14:15') – kompakt machen."""
    if isinstance(val, (int, float)):
        secs = int(val)
        d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    else:
        text = str(val)
        d = int(re.search(r"(\d+)\s+day", text).group(1)) if re.search(r"(\d+)\s+day", text) else 0
        tm = re.search(r"(\d+):(\d+):(\d+)", text)
        h = int(tm.group(1)) if tm else 0
        m = int(tm.group(2)) if tm else 0
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _build_system_metrics(name, cpu, mem, load, uptime, sensors, temp_warn=70, temp_crit=85):
    metrics = []
    if cpu and cpu.get("total") is not None:
        metrics.append({"key": "cpu", "label": "CPU", "kind": "percent",
                        "value": round(float(cpu["total"]), 1)})
    if mem and mem.get("percent") is not None:
        used, total = mem.get("used"), mem.get("total")
        detail = f"{_gb(used)} / {_gb(total)} GB" if used and total else None
        metrics.append({"key": "mem", "label": "RAM", "kind": "percent",
                        "value": round(float(mem["percent"]), 1), "detail": detail})
    if load and load.get("min1") is not None:
        metrics.append({"key": "load", "label": "Load", "kind": "text",
                        "value": f'{load.get("min1", 0):.2f} · '
                                 f'{load.get("min5", 0):.2f} · {load.get("min15", 0):.2f}'})
    if uptime:
        metrics.append({"key": "uptime", "label": "Uptime", "kind": "text",
                        "value": _fmt_uptime_str(uptime)})
    if sensors:
        temps = [
            s for s in sensors
            if isinstance(s.get("value"), (int, float))
            and (s.get("unit") in ("C", "°C") or "temp" in str(s.get("type", "")).lower())
        ]
        if temps:
            hottest = max(temps, key=lambda s: s["value"])
            tval = round(hottest["value"])
            tstatus = "crit" if tval >= temp_crit else ("warn" if tval >= temp_warn else "ok")
            metrics.append({"key": "temp", "label": "Temp", "kind": "temp",
                            "value": tval, "unit": "°C", "status": tstatus,
                            "detail": str(hottest.get("label", "")) or None})
    return {"enabled": True, "name": name, "ok": True, "error": None, "metrics": metrics}


async def _glances_get(client, base, ver, endpoint):
    resp = await client.get(f"{base}/api/{ver}/{endpoint}")
    resp.raise_for_status()
    return resp.json()


async def _safe(coro):
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return None


async def get_system_stats(cfg: dict) -> dict:
    name = cfg.get("name", "System")
    base = str(cfg.get("url", "")).rstrip("/")
    if not base:
        return {"enabled": True, "name": name, "ok": False, "error": "keine url", "metrics": []}

    timeout = float(cfg.get("timeout", 4))
    insecure = bool(cfg.get("insecure", False))
    ver = cfg.get("api_version")

    try:
        async with httpx.AsyncClient(verify=not insecure, timeout=timeout) as client:
            # API-Version automatisch bestimmen (Glances 4.x -> /api/4, 3.x -> /api/3)
            if not ver:
                ver = 4
                try:
                    await _glances_get(client, base, 4, "cpu")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        ver = 3

            cpu, mem, load, uptime, sensors = await asyncio.gather(
                _safe(_glances_get(client, base, ver, "cpu")),
                _safe(_glances_get(client, base, ver, "mem")),
                _safe(_glances_get(client, base, ver, "load")),
                _safe(_glances_get(client, base, ver, "uptime")),
                _safe(_glances_get(client, base, ver, "sensors")),
            )
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "name": name, "ok": False,
                "error": type(exc).__name__, "metrics": []}

    if cpu is None or mem is None:
        return {"enabled": True, "name": name, "ok": False,
                "error": "Glances nicht erreichbar / falsche API-Version", "metrics": []}

    temp_warn = float(cfg.get("temp_warn", 70))
    temp_crit = float(cfg.get("temp_crit", 85))
    result = _build_system_metrics(name, cpu, mem, load, uptime, sensors, temp_warn, temp_crit)

    # Temperatur-Alarm: bei Überschreiten von temp_crit oben den roten Alarm auslösen
    if cfg.get("temp_alarm", True):
        temp_metric = next((m for m in result["metrics"] if m["key"] == "temp"), None)
        if temp_metric and temp_metric["value"] >= temp_crit:
            result["alarm"] = True
            result["alarm_label"] = f'{name} {temp_metric["value"]} °C'

    return result
