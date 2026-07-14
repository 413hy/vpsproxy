#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run as root." >&2; exit 1; fi
systemctl disable --now vps-proxy-manager.service 2>/dev/null || true
rm -f /etc/systemd/system/vps-proxy-manager.service
systemctl daemon-reload
echo "Service removed. Data remains in /opt/vps-proxy-manager/data and config remains in /etc/vps-proxy-manager."
