#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/vps-proxy-manager
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi
systemctl stop vps-proxy-manager.service 2>/dev/null || true
rsync -a --delete --exclude '.env' "$SRC_DIR/" "$APP_DIR/"
"$APP_DIR/venv/bin/pip" install --upgrade -e "$APP_DIR"
chown -R vpspm:vpspm "$APP_DIR"
systemctl daemon-reload
systemctl start vps-proxy-manager.service
echo "Upgraded."
