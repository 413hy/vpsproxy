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
  systemctl disable --now sing-box.service 2>/dev/null || true
  echo "No rollback script was found; sing-box was stopped and its config was preserved." >&2
fi
echo "Emergency restore attempted."
