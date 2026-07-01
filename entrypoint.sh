#!/bin/sh
# Legt beim ersten Start eine Standard-config.yml an, falls noch keine
# vorhanden ist (z. B. frisches appdata-Verzeichnis auf Unraid). Danach
# wird der eigentliche Befehl (uvicorn) gestartet.
set -e

CONFIG="${CONFIG_PATH:-/config/config.yml}"
DEFAULT_CONFIG="${DEFAULT_CONFIG:-/srv/config.default.yml}"

mkdir -p "$(dirname "$CONFIG")"

if [ ! -f "$CONFIG" ]; then
  echo "[entrypoint] Keine config.yml gefunden – lege Standard an: $CONFIG"
  cp "$DEFAULT_CONFIG" "$CONFIG"
fi

exec "$@"
