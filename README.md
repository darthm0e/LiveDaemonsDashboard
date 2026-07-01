# LDD – Live Daemons Dashboard

Eine kleine, selbst gehostete Übersicht für Docker-Container, Dienste und VMs.
Zeigt auf einen Blick, was läuft und was ausgefallen ist – mit IP, Latenz und
einem roten Alarm, sobald ein als **kritisch** markierter Dienst offline geht.

- **Docker-Container** werden automatisch über den Docker-Socket erkannt
  (Status, Health, IP, Image).
- **Dienste / VMs / Geräte** prüfst du frei konfigurierbar per `http`, `tcp`
  oder `ping`. Eine VM gilt als „up", wenn sie auf ihrer IP antwortet.
- Das Frontend pollt im konfigurierten Intervall, kein externes CDN, läuft
  also auch in einem abgeschotteten Netz.

## Schnellstart

```bash
docker compose up -d --build
```

Dann öffnen: `http://<host>:8080`

Konfiguration liegt in `config.yml` und wird bei **jedem** Abruf neu gelesen –
Änderungen wirken sofort, kein Neustart nötig.

## Auf Unraid installieren / an andere verteilen

Für Unraid gibt es das Template `ldd.xml`. Damit andere es nutzen
können, muss das Image in einer Registry liegen – nicht mehr lokal gebaut werden.

**1. Image veröffentlichen** – zwei Wege:

- *Automatisch (empfohlen):* Repo zu GitHub pushen. Der mitgelieferte Workflow
  `.github/workflows/docker-publish.yml` baut bei jedem Push ein Multi-Arch-Image
  und legt es unter `ghcr.io/DEINUSER/ldd:latest` ab. Danach das
  Package in den GitHub-Einstellungen auf **public** stellen.
- *Manuell (Docker Hub):*
  ```bash
  docker build -t DEINUSER/ldd:latest .
  docker push DEINUSER/ldd:latest
  ```

**2. Template anpassen** – in `ldd.xml` und `icon.png` überall
`DEINUSER` durch deinen Docker-Hub- bzw. GitHub-Namen ersetzen. Das `<Icon>`
zeigt auf die `icon.png` im Repo (roher GitHub-Link).

**3. Installieren:**

- *Bei dir selbst / zum Testen:* die `ldd.xml` auf den Unraid-Server
  nach `/boot/config/plugins/dockerMan/templates-user/` kopieren. Dann im
  Docker-Tab unten auf **Add Container** → oben unter *Template* erscheint
  „LDD".
- *Für alle (Community Applications):* das Repo bei den
  [CA-Templates](https://forums.unraid.net/topic/57181-real-docker-faq/) als
  Template-Repository einreichen bzw. deinen Template-Repo-Link angeben. Dann
  finden andere es per Suche in *Apps*.

Beim ersten Start legt der Container automatisch eine Standard-`config.yml` im
gewählten appdata-Ordner an (Standard `/mnt/user/appdata/ldd`).
Die kann dann direkt über den Unraid-Dateimanager oder per Editor angepasst
werden – Änderungen wirken sofort.

## Konfiguration

```yaml
settings:
  title: "LDD"
  refresh_interval: 30        # Sekunden

docker:
  enabled: true
  show_all: true              # auch gestoppte Container zeigen
  critical: [traefik, postgres]   # diese lösen den roten Alarm aus

groups:
  - name: "Kritische Dienste"
    checks:
      - name: "UniFi Controller"
        type: http
        target: "https://192.168.0.8:8443"
        insecure: true         # selbstsigniertes Zertifikat zulassen
        critical: true
      - name: "FritzBox"
        type: ping
        target: "192.168.178.1"
      - name: "PostgreSQL"
        type: tcp
        target: "192.168.0.10:5432"
```

### Check-Typen

| type   | `target`            | „up", wenn …                                  | Optionen |
|--------|---------------------|-----------------------------------------------|----------|
| `http` | volle URL           | Antwort kommt (Status < 500)                  | `insecure`, `expect_status: [200]`, `timeout` |
| `tcp`  | `host:port`         | Port nimmt Verbindung an                      | `timeout` |
| `ping` | `host` oder IP      | ICMP-Antwort kommt zurück                     | `timeout` |

Jeder Check kennt zusätzlich `critical: true` – nur solche Ausfälle färben die
Statusleiste rot und erscheinen namentlich oben.

## Zustände in der Oberfläche

- **Alles stabil** – alles erreichbar.
- **Stabil, mit Ausfällen** (gelb) – etwas ist offline, aber nichts Kritisches.
- **N kritisch ausgefallen** (rot) – mindestens ein kritischer Dienst ist down;
  die Namen stehen oben.

Docker-Container, die laufen, aber als *unhealthy* gemeldet werden, erscheinen
gelb (degradiert).

## Netzwerk

Im Standard-`bridge`-Modus erreicht der Container LAN-Geräte und VMs für
`ping`/`tcp`/`http` per Routing problemlos. Wenn du echte LAN-IPs sehen oder das
Dashboard ohne Port-Mapping auf der Host-IP erreichbar machen willst, aktiviere
`network_mode: host` in der `docker-compose.yml`.

Falls `ping` nichts zurückgibt, fehlt dem Container evtl. die ICMP-Berechtigung.
In dem Fall in der `compose`-Datei ergänzen:

```yaml
    cap_add:
      - NET_RAW
```

## Passwortschutz

Optionaler Login über HTTP Basic Auth – ein Benutzer, in der `config.yml`:

```yaml
auth:
  enabled: true
  username: "moe"
  password: "geheim123"
```

Ist er aktiv, fragt der Browser beim Öffnen nach Benutzer und Passwort und merkt
sie sich bis zum Schließen. Alle Seiten und die API sind geschützt; nur
`/healthz` (für den Docker-Healthcheck) bleibt offen.

Das Passwort steht **im Klartext** in der Datei – für ein einzelnes LAN-Login in
Ordnung, aber: Datei entsprechend schützen und Basic Auth am besten nur über
HTTPS nutzen (die Zugangsdaten werden sonst nur base64-kodiert übertragen). Am
saubersten hinter einem Reverse-Proxy mit TLS. Ein „Logout" gibt es bei Basic
Auth nicht direkt – dafür den Browser schließen bzw. das Fenster neu
authentifizieren lassen.

## Sicherheit / Hardening

Der gemountete Docker-Socket gibt der App effektiv **volle Kontrolle** über den
Docker-Host – auch wenn er hier nur gelesen wird. Wenn dir das zu viel ist,
schalte einen Read-only-Proxy davor (z. B. `tecnativa/docker-socket-proxy`) und
zeige nur `CONTAINERS=1` frei, statt den echten Socket einzuhängen.

Das Dashboard hat keine Authentifizierung. Stell es nicht ungeschützt ins
Internet – sondern hinter Reverse-Proxy mit Auth (z. B. Authelia) oder nur im
LAN/VPN erreichbar.

## System-Vitaldaten (Unraid via Glances)

Das Panel oben zeigt CPU, RAM, Load, Uptime und Temperatur deines Servers.
Datenquelle ist **Glances** im Webserver-Modus (liefert eine REST-API).

**Auf dem Unraid-Server einrichten** – am einfachsten über *Community
Applications* nach `Glances` suchen und installieren. Wichtig ist, dass Glances
im Web-/REST-Modus läuft (Port **61208**). Alternativ als Container:

```bash
docker run -d --restart=unless-stopped \
  --name glances --pid host -p 61208:61208 \
  -e GLANCES_OPT="-w" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  nicolargo/glances:latest
```

`--pid host` ist nötig, damit Glances die echten Host-Werte sieht, nicht nur
die des Containers.

Dann im Dashboard die `config.yml` anpassen:

```yaml
system:
  enabled: true
  name: "Unraid"
  url: "http://192.168.0.3:61208"   # IP/Port deines Glances
```

Die API-Version wird automatisch erkannt (Glances 4.x → `/api/4`, 3.x →
`/api/3`). Schwellen für die Farbanzeige: CPU/RAM grün < 70 %, gelb ab 70 %, rot
ab 90 %. Die **Temperatur** ist über `temp_warn` / `temp_crit` einstellbar
(Standard 70 / 85 °C): grün darunter, gelb ab `temp_warn`, rot ab `temp_crit`.
Mit `temp_alarm: true` löst ein Überschreiten von `temp_crit` zusätzlich den
roten Alarm-Balken oben aus und nennt den Wert (z. B. „Unraid 92 °C"). Auf
`false` setzen, wenn die Kachel zwar rot werden, aber keinen Alarm auslösen soll.

**Hinweis Temperaturen:** In einem Container sieht Glances Sensoren nur
eingeschränkt. Wenn keine Temperatur erscheint, nutze auf Unraid stattdessen das
Glances-*Plugin* (läuft direkt auf dem Host) – dann sind die Sensoren da. Die
übrigen Werte (CPU/RAM/Load/Uptime) funktionieren in beiden Varianten.

## VM-Status über den Hypervisor (optional)

`ping`/`tcp` zeigen nur, ob die VM **antwortet**. Wenn du stattdessen den vom
Hypervisor gemeldeten Zustand (an/aus/pausiert) willst, lässt sich in
`app/checks.py` ein zusätzlicher Typ andocken – z. B. die Proxmox-API
(`/api2/json/nodes/<node>/qemu/<vmid>/status/current`) oder libvirt. Sag
Bescheid, dann ergänze ich den passenden Typ.
```
