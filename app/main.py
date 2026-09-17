"""LDD – Live Daemons Dashboard – Backend.

Serviert das Frontend und eine JSON-API unter /api/status. Die Konfiguration
wird bei jedem Abruf frisch aus config.yml gelesen, damit Änderungen ohne
Neustart wirken.
"""

import asyncio
import os
import secrets
import time
from pathlib import Path

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .checks import get_calendar_events, get_docker_status, get_system_stats, run_check

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yml")
STATIC_DIR = Path(__file__).parent / "static"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        return {"_error": f"config.yml ungültig: {exc}"}


# ---- Optionaler Passwortschutz (HTTP Basic Auth, ein Benutzer) ----
security = HTTPBasic(auto_error=False)


def _eq(a: str, b: str) -> bool:
    # zeitkonstant vergleichen; utf-8, damit auch Sonderzeichen im Passwort gehen
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_auth(request: Request,
                 credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if request.url.path in ("/healthz", "/favicon.svg", "/api/agent/report"):  # immer offen
        return
    auth = load_config().get("auth", {}) or {}
    if not auth.get("enabled"):              # Schutz deaktiviert -> alles frei
        return
    user = str(auth.get("username", "admin"))
    pw = str(auth.get("password", ""))
    if credentials and _eq(credentials.username, user) and _eq(credentials.password, pw):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Nicht autorisiert",
        headers={"WWW-Authenticate": "Basic"},
    )


app = FastAPI(title="LDD – Live Daemons Dashboard", dependencies=[Depends(require_auth)])


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/favicon.svg")
async def favicon():
    return Response(
        (STATIC_DIR / "favicon.svg").read_text(encoding="utf-8"),
        media_type="image/svg+xml",
    )


# ---- VPN-Agent: Meldungen der VMs (Heartbeat/Push) ----
# In-Memory-Speicher: agent-id -> {vpn, vpn_ip, device, ts}. Übersteht keinen
# Neustart, aber die Agents melden sich im Minutentakt neu.
_agent_reports: dict = {}


@app.post("/api/agent/report")
async def agent_report(payload: dict = Body(...)):
    cfg = load_config()
    vpn_cfg = cfg.get("vpn", {}) or {}
    expected = str(vpn_cfg.get("agent_token", ""))
    given = str(payload.get("token", ""))
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ungültiger Agent-Token")

    agent_id = str(payload.get("id", "")).strip()
    if not agent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="id fehlt")

    _agent_reports[agent_id] = {
        "vpn": bool(payload.get("vpn", False)),
        "vpn_ip": (str(payload.get("vpn_ip", "")).strip() or None),
        "device": (str(payload.get("device", "")).strip() or None),
        "ts": time.time(),
    }
    return {"status": "ok"}


def build_vpn_status(vpn_cfg: dict) -> dict:
    """Baut den VPN-Status aus den konfigurierten Mitgliedern + den Agent-Meldungen."""
    timeout = float(vpn_cfg.get("timeout", 180))
    now = time.time()
    members_out = []
    for m in vpn_cfg.get("members", []) or []:
        mid = str(m.get("id", "")).strip()
        rep = _agent_reports.get(mid)
        critical = bool(m.get("critical", False))
        if rep is None or (now - rep["ts"]) > timeout:
            state, connected, vpn_ip, device, since = "offline", False, None, None, None
        else:
            connected = rep["vpn"]
            state = "connected" if connected else "disconnected"
            vpn_ip = rep["vpn_ip"]
            device = rep["device"]
            since = rep["ts"]
        members_out.append({
            "name": m.get("name", mid or "VM"),
            "state": state,             # connected | disconnected | offline
            "connected": connected,
            "vpn_ip": vpn_ip,
            "device": device,
            "service": (str(m.get("service", "")).strip() or None),
            "last_seen": (rep["ts"] if rep else None),
            "critical": critical,
        })
    return {"enabled": True, "members": members_out}


@app.get("/api/config")
async def api_config():
    cfg = load_config()
    settings = cfg.get("settings", {}) or {}
    return {
        "title": settings.get("title", "LDD"),
        "refresh_interval": int(settings.get("refresh_interval", 30)),
        "config_error": cfg.get("_error"),
    }


@app.get("/api/status")
async def api_status():
    cfg = load_config()
    loop = asyncio.get_event_loop()

    # --- Docker ---
    docker_cfg = cfg.get("docker", {}) or {}
    docker_result = {"enabled": False, "containers": [], "error": None}
    if docker_cfg.get("enabled", True):
        docker_result = await loop.run_in_executor(
            None,
            get_docker_status,
            docker_cfg.get("show_all", True),
            docker_cfg.get("critical", []),
        )

    # --- Konfigurierte Checks (Dienste / VMs / ...) ---
    groups_cfg = cfg.get("groups", []) or []
    groups_out = []
    tasks = []
    slots = []  # (group_index, check_index)

    for gi, group in enumerate(groups_cfg):
        checks = group.get("checks", []) or []
        groups_out.append(
            {"name": group.get("name", "Dienste"), "checks": [None] * len(checks)}
        )
        for ci, chk in enumerate(checks):
            tasks.append(run_check(chk))
            slots.append((gi, ci))

    results = await asyncio.gather(*tasks) if tasks else []
    for (gi, ci), result in zip(slots, results):
        groups_out[gi]["checks"][ci] = result

    # --- System-Vitaldaten (Unraid via Glances) ---
    system_cfg = cfg.get("system", {}) or {}
    if system_cfg.get("enabled"):
        system_result = await get_system_stats(system_cfg)
    else:
        system_result = {"enabled": False, "metrics": []}

    # --- Kalender-Termine (optional) ---
    calendar_cfg = cfg.get("calendar", {}) or {}
    if calendar_cfg.get("enabled"):
        calendar_result = await get_calendar_events(calendar_cfg)
    else:
        calendar_result = {"enabled": False, "events": []}

    # --- VPN-Status der VMs (via Agent) ---
    vpn_cfg = cfg.get("vpn", {}) or {}
    if vpn_cfg.get("enabled"):
        vpn_result = build_vpn_status(vpn_cfg)
        # Optional: den VPN-Server selbst auf Erreichbarkeit prüfen
        server_cfg = vpn_cfg.get("server") or {}
        if server_cfg.get("target"):
            vpn_result["server"] = await run_check({
                "name": server_cfg.get("name", "VPN-Server"),
                "type": server_cfg.get("type", "ping"),
                "target": server_cfg.get("target"),
                "timeout": server_cfg.get("timeout", 3),
                "insecure": bool(server_cfg.get("insecure", False)),
                "critical": bool(server_cfg.get("critical", False)),
            })
    else:
        vpn_result = {"enabled": False, "members": []}

    # --- Zusammenfassung ---
    up = down = total = 0
    critical_down = []

    for cont in docker_result["containers"]:
        total += 1
        if cont["ok"]:
            up += 1
        else:
            down += 1
            if cont["critical"]:
                critical_down.append(cont["name"])

    for group in groups_out:
        for chk in group["checks"]:
            total += 1
            if chk["ok"]:
                up += 1
            else:
                down += 1
                if chk["critical"]:
                    critical_down.append(chk["name"])

    # VPN: getrennte/offline Mitglieder zählen mit; kritische lösen den Alarm aus
    for m in vpn_result["members"]:
        total += 1
        if m["connected"]:
            up += 1
        else:
            down += 1
            if m["critical"]:
                label = m["name"] + (" (offline)" if m["state"] == "offline" else " (VPN getrennt)")
                critical_down.append(label)

    # VPN-Server-Kachel (falls konfiguriert)
    srv = vpn_result.get("server")
    if srv:
        total += 1
        if srv["ok"]:
            up += 1
        else:
            down += 1
            if srv["critical"]:
                critical_down.append(srv["name"] + " (VPN-Server)")

    # System-Temperatur kann den kritischen Alarm oben auslösen
    if system_result.get("alarm"):
        critical_down.append(system_result.get("alarm_label", "System-Temperatur"))

    return {
        "generated_at": time.time(),
        "summary": {
            "up": up,
            "down": down,
            "total": total,
            "critical_down": critical_down,
        },
        "docker": docker_result,
        "groups": groups_out,
        "system": system_result,
        "calendar": calendar_result,
        "vpn": vpn_result,
    }
