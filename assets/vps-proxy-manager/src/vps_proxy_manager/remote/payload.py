from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BASE = Path("/etc/vps-proxy-manager")
BACKUPS = BASE / "backups"
SINGBOX_CONFIG = Path("/etc/sing-box/config.json")
ROLLBACK_SCRIPT = BASE / "rollback-last.sh"
ROLLBACK_SERVICE = Path("/etc/systemd/system/vpspm-rollback.service")
ROLLBACK_TIMER = Path("/etc/systemd/system/vpspm-rollback.timer")


def run(argv: list[str], *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, text=True, capture_output=True, timeout=timeout)


def ok(data: dict[str, Any] | None = None) -> None:
    print(json.dumps({"ok": True, **(data or {})}, ensure_ascii=False))


def fail(code: str, message: str, detail: str = "") -> None:
    print(json.dumps({"ok": False, "code": code, "message": message, "detail": detail}, ensure_ascii=False))
    raise SystemExit(0)


def read_input() -> dict[str, Any]:
    if "_PAYLOAD_DATA" in globals():
        return json.loads(str(globals()["_PAYLOAD_DATA"]))
    text = sys.stdin.read()
    marker = "\n# VPSPM_STDIN\n"
    if marker in text:
        text = text.split(marker, 1)[1]
    if not text.strip():
        return {}
    return json.loads(text)


def detect() -> None:
    os_release: dict[str, str] = {}
    release_path = Path("/etc/os-release")
    if release_path.exists():
        for line in release_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    ssh_client = os.environ.get("SSH_CLIENT", "").split()
    default_route = run(["ip", "-j", "route", "show", "default"], check=False).stdout
    tools = {name: shutil.which(name) is not None for name in ["sing-box", "nft", "iptables", "systemctl", "curl"]}
    ok(
        {
            "system": {
                "os_release": os_release,
                "arch": platform.machine(),
                "kernel": platform.release(),
                "python": platform.python_version(),
                "ssh_client_ip": ssh_client[0] if ssh_client else None,
                "default_route": default_route,
                "tools": tools,
            }
        }
    )


def ensure_layout() -> None:
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path("/etc/sing-box").mkdir(mode=0o755, parents=True, exist_ok=True)


def backup_state() -> Path:
    ensure_layout()
    version = time.strftime("%Y%m%d%H%M%S")
    dest = BACKUPS / version
    dest.mkdir(mode=0o700)
    files = [SINGBOX_CONFIG, Path("/etc/resolv.conf")]
    for src in files:
        if src.exists() and not src.is_symlink():
            shutil.copy2(src, dest / src.name.replace("/", "_"))
    commands = {
        "ip_route.txt": ["ip", "route", "show", "table", "all"],
        "ip_rule.txt": ["ip", "rule", "show"],
        "nft.txt": ["nft", "list", "ruleset"],
        "systemctl.txt": ["systemctl", "status", "sing-box", "--no-pager"],
    }
    for filename, argv in commands.items():
        if shutil.which(argv[0]):
            (dest / filename).write_text(run(argv, check=False).stdout, encoding="utf-8")
    (BASE / "last-backup").write_text(str(dest), encoding="utf-8")
    return dest


def install_singbox() -> None:
    if shutil.which("sing-box"):
        return
    release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    if "ID=debian" not in release and "ID=ubuntu" not in release:
        fail("unsupported_system", "当前版本仅自动支持 Debian/Ubuntu")
    if not shutil.which("curl"):
        run(["apt-get", "update"], timeout=180)
        run(["apt-get", "install", "-y", "curl", "ca-certificates", "iproute2", "nftables"], timeout=300)
    installer = "https://sing-box.app/deb-install.sh"
    run(["bash", "-lc", f"curl -fsSL {installer} | bash"], timeout=300)
    if not shutil.which("sing-box"):
        fail("singbox_install_failed", "sing-box 安装后仍不可用")


def write_rollback(backup: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
systemctl stop sing-box.service 2>/dev/null || true
if [ -f "{backup}/config.json" ]; then
  mkdir -p /etc/sing-box
  cp "{backup}/config.json" /etc/sing-box/config.json
  systemctl restart sing-box.service 2>/dev/null || true
else
  rm -f /etc/sing-box/config.json
fi
systemctl disable --now vpspm-rollback.timer 2>/dev/null || true
"""
    ROLLBACK_SCRIPT.write_text(script, encoding="utf-8")
    ROLLBACK_SCRIPT.chmod(0o700)


def arm_rollback(seconds: int) -> None:
    ROLLBACK_SERVICE.write_text(
        "[Unit]\nDescription=VPS Proxy Manager emergency rollback\n"
        "[Service]\nType=oneshot\nExecStart=/etc/vps-proxy-manager/rollback-last.sh\n",
        encoding="utf-8",
    )
    ROLLBACK_TIMER.write_text(
        "[Unit]\nDescription=VPS Proxy Manager rollback timer\n"
        "[Timer]\nOnActiveSec=%s\nUnit=vpspm-rollback.service\n"
        "[Install]\nWantedBy=timers.target\n" % seconds,
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"], check=False)
    run(["systemctl", "enable", "--now", "vpspm-rollback.timer"], check=False)


def disarm_rollback() -> None:
    run(["systemctl", "disable", "--now", "vpspm-rollback.timer"], check=False)


def write_service() -> None:
    service = Path("/etc/systemd/system/sing-box.service")
    service.write_text(
        "[Unit]\nDescription=sing-box service managed by VPS Proxy Manager\n"
        "After=network-online.target nss-lookup.target\nWants=network-online.target\n"
        "[Service]\nUser=root\nCapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
        "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
        "ExecStart=/usr/bin/sing-box run -c /etc/sing-box/config.json\n"
        "ExecReload=/bin/kill -HUP $MAINPID\nRestart=on-failure\nRestartSec=10\nLimitNOFILE=infinity\n"
        "[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"], check=False)


def apply_proxy() -> None:
    data = read_input()
    config = data.get("config")
    if not isinstance(config, dict):
        fail("bad_request", "缺少 sing-box 配置")
    rollback_seconds = int(data.get("rollback_seconds") or 120)
    ensure_layout()
    backup = backup_state()
    write_rollback(backup)
    install_singbox()
    write_service()
    tmp = Path(tempfile.mkstemp(prefix="vpspm_singbox_", suffix=".json")[1])
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    check = run(["sing-box", "check", "-c", str(tmp)], check=False)
    if check.returncode != 0:
        tmp.unlink(missing_ok=True)
        fail("singbox_check_failed", "sing-box 配置校验失败", check.stderr[-1200:])
    shutil.copy2(tmp, SINGBOX_CONFIG)
    SINGBOX_CONFIG.chmod(0o600)
    tmp.unlink(missing_ok=True)
    arm_rollback(rollback_seconds)
    started = run(["systemctl", "enable", "--now", "sing-box.service"], check=False, timeout=90)
    if started.returncode != 0:
        run([str(ROLLBACK_SCRIPT)], check=False)
        fail("singbox_start_failed", "sing-box 启动失败，已尝试回滚", started.stderr[-1200:])
    ok({"backup": str(backup), "rollback_armed": True})


def confirm_proxy() -> None:
    disarm_rollback()
    ok({"rollback_armed": False})


def rollback() -> None:
    if not ROLLBACK_SCRIPT.exists():
        fail("no_backup", "没有可用回滚脚本")
    result = run([str(ROLLBACK_SCRIPT)], check=False)
    if result.returncode != 0:
        fail("rollback_failed", "回滚执行失败", result.stderr[-1200:])
    ok({"rolled_back": True})


def stop_proxy() -> None:
    disarm_rollback()
    run(["systemctl", "disable", "--now", "sing-box.service"], check=False)
    ok({"stopped": True, "exit_mode": "local", "persistent": True})


def restore_proxy() -> None:
    result = run(["systemctl", "enable", "--now", "sing-box.service"], check=False)
    if result.returncode != 0:
        fail("restore_failed", "代理服务恢复失败", result.stderr[-1200:])
    ok({"running": True, "exit_mode": "proxy", "persistent": True})


def uninstall() -> None:
    backup_state()
    run(["systemctl", "disable", "--now", "sing-box.service"], check=False)
    run(["systemctl", "disable", "--now", "vpspm-rollback.timer"], check=False)
    SINGBOX_CONFIG.unlink(missing_ok=True)
    ok({"uninstalled": True})


def status() -> None:
    service = run(["systemctl", "is-active", "sing-box.service"], check=False)
    version = run(["sing-box", "version"], check=False).stdout.splitlines()[0] if shutil.which("sing-box") else ""
    outbound = run(["curl", "-4fsS", "--max-time", "8", "https://ifconfig.co/json"], check=False)
    ok(
        {
            "status": {
                "singbox_active": service.stdout.strip(),
                "singbox_version": version,
                "outbound_probe": outbound.stdout[:800] if outbound.returncode == 0 else "",
                "outbound_error": outbound.stderr[:400] if outbound.returncode != 0 else "",
                "has_backup": (BASE / "last-backup").exists(),
            }
        }
    )


def speedtest() -> None:
    data = read_input()
    config = data.get("config")
    port = int(data.get("listen_port") or 18080)
    if not isinstance(config, dict):
        fail("bad_request", "缺少测速配置")
    install_singbox()
    server = config["outbounds"][0]["server"]
    started = time.monotonic()
    dns_ok = False
    tcp_ok = False
    error = ""
    try:
        addr = socket.getaddrinfo(server, None, type=socket.SOCK_STREAM)[0][4][0]
        dns_ok = True
        with socket.create_connection((addr, int(config["outbounds"][0]["server_port"])), timeout=5):
            tcp_ok = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    cfg = Path(tempfile.mkstemp(prefix="vpspm_speed_", suffix=".json")[1])
    cfg.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen(["sing-box", "run", "-c", str(cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1.5)
        curl_start = time.monotonic()
        curl = run(
            [
                "curl",
                "-x",
                f"http://127.0.0.1:{port}",
                "-fsS",
                "-o",
                "/dev/null",
                "-w",
                "%{time_connect} %{time_appconnect} %{time_total}",
                "--max-time",
                "15",
                "https://www.gstatic.com/generate_204",
            ],
            check=False,
            timeout=20,
        )
        access_ms = int((time.monotonic() - curl_start) * 1000)
        ok(
            {
                "result": {
                    "dns_ok": dns_ok,
                    "tcp_ok": tcp_ok,
                    "proxy_ok": curl.returncode == 0,
                    "latency_ms": access_ms if curl.returncode == 0 else None,
                    "curl_timing": curl.stdout.strip(),
                    "error": "" if curl.returncode == 0 else (curl.stderr.strip() or error),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            }
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        cfg.unlink(missing_ok=True)


def main() -> None:
    if len(sys.argv) < 2:
        fail("bad_request", "missing action")
    actions = {
        "detect": detect,
        "apply_proxy": apply_proxy,
        "confirm_proxy": confirm_proxy,
        "rollback": rollback,
        "stop_proxy": stop_proxy,
        "restore_proxy": restore_proxy,
        "uninstall": uninstall,
        "status": status,
        "speedtest": speedtest,
    }
    action = actions.get(sys.argv[1])
    if action is None:
        fail("bad_request", "unknown action")
    action()


if __name__ == "__main__":
    main()
