#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/vps-proxy-manager
ENV_DIR=/etc/vps-proxy-manager
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates curl openssh-client rsync

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.11+ is required. Install python3.11 or set PYTHON_BIN." >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$ENV_DIR"
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
mkdir -p "$APP_DIR/data"
"$PYTHON_BIN" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -e "$APP_DIR"

if ! command -v sing-box >/dev/null 2>&1; then
  INSTALLER="$(mktemp)"
  trap 'rm -f "$INSTALLER"' EXIT
  curl -fL --proto '=https' --tlsv1.2 --max-time 60 -o "$INSTALLER" https://sing-box.app/deb-install.sh
  bash "$INSTALLER"
  systemctl disable --now sing-box.service 2>/dev/null || true
  rm -f "$INSTALLER"
  trap - EXIT
fi

if [ ! -f "$ENV_DIR/vps-proxy-manager.env" ]; then
  cp "$APP_DIR/.env.example" "$ENV_DIR/vps-proxy-manager.env"
  echo "Created $ENV_DIR/vps-proxy-manager.env. Fill token, admin IDs, and secret key before starting."
fi

chown -R root:root "$APP_DIR"
chmod 700 "$APP_DIR/data"
chown root:root "$ENV_DIR" "$ENV_DIR/vps-proxy-manager.env"
chmod 700 "$ENV_DIR"
chmod 600 "$ENV_DIR/vps-proxy-manager.env"
mkdir -p /root/.codex/skills/vps-proxy-target-bootstrap
rsync -a --delete "$APP_DIR/codex-skills/vps-proxy-target-bootstrap/" /root/.codex/skills/vps-proxy-target-bootstrap/
chmod -R go-rwx /root/.codex/skills/vps-proxy-target-bootstrap
"$APP_DIR/venv/bin/vps-proxy-manager" init-db
cp "$APP_DIR/systemd/vps-proxy-manager.service" /etc/systemd/system/vps-proxy-manager.service
cp "$APP_DIR/systemd/vps-proxy-codex-worker.service" /etc/systemd/system/vps-proxy-codex-worker.service
systemctl daemon-reload
echo "Installed. Edit $ENV_DIR/vps-proxy-manager.env, verify 'codex login status', then run:"
echo "  systemctl enable --now vps-proxy-manager.service vps-proxy-codex-worker.service"
