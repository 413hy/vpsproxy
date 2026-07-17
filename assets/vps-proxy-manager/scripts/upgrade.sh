#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/vps-proxy-manager
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi
systemctl stop vps-proxy-codex-worker.service vps-proxy-manager.service 2>/dev/null || true
if [ -f "$APP_DIR/data/app.db" ]; then
  cp -a "$APP_DIR/data/app.db" "$APP_DIR/data/app.db.before-upgrade.$(date +%Y%m%d%H%M%S)"
fi
rsync -a --delete \
  --exclude '.env' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'data/' \
  --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.egg-info/' \
  "$SRC_DIR/" "$APP_DIR/"
"$APP_DIR/venv/bin/pip" install --upgrade -e "$APP_DIR"
mkdir -p /root/.codex/skills/vps-proxy-target-bootstrap
rsync -a --delete "$APP_DIR/codex-skills/vps-proxy-target-bootstrap/" /root/.codex/skills/vps-proxy-target-bootstrap/
chmod -R go-rwx /root/.codex/skills/vps-proxy-target-bootstrap
"$APP_DIR/venv/bin/vps-proxy-manager" init-db
chown -R root:root "$APP_DIR"
cp "$APP_DIR/systemd/vps-proxy-manager.service" /etc/systemd/system/vps-proxy-manager.service
cp "$APP_DIR/systemd/vps-proxy-codex-worker.service" /etc/systemd/system/vps-proxy-codex-worker.service
systemctl daemon-reload
systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service
echo "Upgraded."
