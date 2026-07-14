#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run on the target VPS as root." >&2
  exit 1
fi

systemctl disable --now vpspm-rollback.timer 2>/dev/null || true
if [ -x /etc/vps-proxy-manager/rollback-last.sh ]; then
  /etc/vps-proxy-manager/rollback-last.sh
else
  systemctl stop sing-box.service 2>/dev/null || true
  rm -f /etc/sing-box/config.json
fi
systemctl restart systemd-networkd 2>/dev/null || true
systemctl restart NetworkManager 2>/dev/null || true
echo "Emergency restore attempted."
