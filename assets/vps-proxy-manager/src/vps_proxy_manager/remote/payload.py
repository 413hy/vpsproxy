from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

AGENT_VERSION = "0.3.2"
BASE = Path("/etc/vps-proxy-manager")
BACKUPS = BASE / "backups"
LIBRARY = BASE / "library"
NODE_LIBRARY = LIBRARY / "nodes"
SUBSCRIPTION_LIBRARY = LIBRARY / "subscriptions"
SINGBOX_CONFIG = Path("/etc/sing-box/config.json")
ROLLBACK_SCRIPT = BASE / "rollback-last.sh"
ROLLBACK_SERVICE = Path("/etc/systemd/system/vpspm-rollback.service")
ROLLBACK_TIMER = Path("/etc/systemd/system/vpspm-rollback.timer")
ACTIVATION_STATUS = BASE / "last-activation.json"
PENDING_CONFIG = BASE / "pending-config.json"
ACTIVE_CONFIG = BASE / "active-config.json"
ACTIVATION_SERVICE = Path("/etc/systemd/system/vpspm-activate.service")
ACTIVATION_TIMER = Path("/etc/systemd/system/vpspm-activate.timer")
AGENT_SOURCE = Path("/usr/local/lib/vpspm-agent/agent.py")
AGENT_BIN = Path("/usr/local/sbin/vpspm-agent")
LOCAL_SINGBOX_SERVICE = Path("/etc/systemd/system/sing-box.service")


def run(
    argv: list[str], *, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, text=True, capture_output=True, timeout=timeout)


def ok(data: dict[str, Any] | None = None) -> None:
    print(json.dumps({"ok": True, **(data or {})}, ensure_ascii=False))


def fail(code: str, message: str, detail: str = "") -> None:
    print(
        json.dumps(
            {"ok": False, "code": code, "message": message, "detail": detail}, ensure_ascii=False
        )
    )
    raise SystemExit(0)


def read_input() -> dict[str, Any]:
    if "_PAYLOAD_DATA" in globals():
        return json.loads(str(globals()["_PAYLOAD_DATA"]))
    text = sys.stdin.read()
    if not text.strip():
        return {}
    return json.loads(text)


def detect_system() -> dict[str, Any]:
    os_release: dict[str, str] = {}
    release_path = Path("/etc/os-release")
    if release_path.exists():
        for line in release_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    ssh_client = os.environ.get("SSH_CLIENT", "").split()
    default_route = run(["ip", "-j", "route", "show", "default"], check=False).stdout
    tools = {
        name: shutil.which(name) is not None
        for name in ["sing-box", "nft", "iptables", "systemctl", "curl", "python3"]
    }
    return {
        "os_release": os_release,
        "arch": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "ssh_client_ip": ssh_client[0] if ssh_client else None,
        "default_route": default_route,
        "tun_available": Path("/dev/net/tun").exists(),
        "tools": tools,
    }


def detect() -> None:
    ok(
        {
            "system": detect_system(),
            "agent_version": AGENT_VERSION if AGENT_SOURCE.exists() else None,
        }
    )


def ensure_layout() -> None:
    for path, mode in [
        (BASE, 0o700),
        (BACKUPS, 0o700),
        (LIBRARY, 0o700),
        (NODE_LIBRARY, 0o700),
        (SUBSCRIPTION_LIBRARY, 0o700),
    ]:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        path.chmod(mode)
    SINGBOX_CONFIG.parent.mkdir(mode=0o755, parents=True, exist_ok=True)


def backup_state() -> Path:
    ensure_layout()
    version = time.strftime("%Y%m%d%H%M%S")
    dest = BACKUPS / version
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = BACKUPS / f"{version}-{suffix}"
    dest.mkdir(mode=0o700)
    if SINGBOX_CONFIG.exists() and not SINGBOX_CONFIG.is_symlink():
        shutil.copy2(SINGBOX_CONFIG, dest / "config.json")
    if LOCAL_SINGBOX_SERVICE.exists() and not LOCAL_SINGBOX_SERVICE.is_symlink():
        shutil.copy2(LOCAL_SINGBOX_SERVICE, dest / "sing-box.service")
    if ACTIVE_CONFIG.exists() and not ACTIVE_CONFIG.is_symlink():
        shutil.copy2(ACTIVE_CONFIG, dest / "active-config.json")
    resolv_conf = Path("/etc/resolv.conf")
    if resolv_conf.exists() and not resolv_conf.is_symlink():
        shutil.copy2(resolv_conf, dest / "resolv.conf")
    markers = {
        "service-active": run(
            ["systemctl", "is-active", "--quiet", "sing-box.service"], check=False
        ).returncode
        == 0,
        "service-enabled": run(
            ["systemctl", "is-enabled", "--quiet", "sing-box.service"], check=False
        ).returncode
        == 0,
        "singbox-preexisting": shutil.which("sing-box") is not None,
    }
    for marker, present in markers.items():
        if present:
            (dest / marker).touch(mode=0o600)
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


def original_backup() -> Path | None:
    pointer = BASE / "original-backup"
    if not pointer.exists():
        return None
    candidate = Path(pointer.read_text(encoding="utf-8").strip())
    if candidate.parent != BACKUPS or not candidate.is_dir():
        return None
    return candidate


def restore_backup(backup: Path) -> None:
    if backup.parent != BACKUPS or not backup.is_dir():
        fail("backup_invalid", "恢复备份无效")
    disarm_rollback()
    run(["systemctl", "stop", "sing-box.service"], check=False)
    saved_config = backup / "config.json"
    if saved_config.exists():
        SINGBOX_CONFIG.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(saved_config, SINGBOX_CONFIG)
        SINGBOX_CONFIG.chmod(0o600)
    else:
        SINGBOX_CONFIG.unlink(missing_ok=True)
    saved_service = backup / "sing-box.service"
    if saved_service.exists():
        shutil.copy2(saved_service, LOCAL_SINGBOX_SERVICE)
        LOCAL_SINGBOX_SERVICE.chmod(0o644)
    else:
        LOCAL_SINGBOX_SERVICE.unlink(missing_ok=True)
    saved_active_config = backup / "active-config.json"
    if saved_active_config.exists():
        shutil.copy2(saved_active_config, ACTIVE_CONFIG)
        ACTIVE_CONFIG.chmod(0o600)
    else:
        ACTIVE_CONFIG.unlink(missing_ok=True)
    PENDING_CONFIG.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"], check=False)
    if (backup / "service-enabled").exists():
        run(["systemctl", "enable", "sing-box.service"], check=False)
    else:
        run(["systemctl", "disable", "sing-box.service"], check=False)
    if (backup / "service-active").exists():
        run(["systemctl", "start", "sing-box.service"], check=False)
    else:
        run(["systemctl", "stop", "sing-box.service"], check=False)


def require_supported_system() -> None:
    system = detect_system()
    os_id = str(system["os_release"].get("ID", "")).lower()
    if os_id not in {"debian", "ubuntu"}:
        fail("unsupported_system", "当前版本仅自动支持 Debian/Ubuntu")
    arch = platform.machine().lower()
    if arch not in {"x86_64", "amd64", "aarch64", "arm64"}:
        fail("unsupported_arch", f"暂不支持目标架构：{arch}")


def install_singbox() -> None:
    if shutil.which("sing-box"):
        return
    require_supported_system()
    run(["apt-get", "update"], timeout=240)
    run(
        ["apt-get", "install", "-y", "curl", "ca-certificates", "iproute2", "nftables"],
        timeout=300,
    )
    fd, installer_name = tempfile.mkstemp(prefix="vpspm-singbox-", suffix=".sh")
    os.close(fd)
    installer = Path(installer_name)
    try:
        downloaded = run(
            [
                "curl",
                "-fL",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--max-time",
                "60",
                "-o",
                str(installer),
                "https://sing-box.app/deb-install.sh",
            ],
            check=False,
            timeout=90,
        )
        if downloaded.returncode != 0:
            fail("singbox_download_failed", "sing-box 安装器下载失败", downloaded.stderr[-1200:])
        run(["bash", str(installer)], timeout=300)
    finally:
        installer.unlink(missing_ok=True)
    if not shutil.which("sing-box"):
        fail("singbox_install_failed", "sing-box 安装后仍不可用")


def ensure_tun() -> None:
    if Path("/dev/net/tun").exists():
        return
    if shutil.which("modprobe"):
        run(["modprobe", "tun"], check=False)
    if not Path("/dev/net/tun").exists():
        fail("tun_unavailable", "目标 VPS 没有可用的 /dev/net/tun")


def write_service() -> None:
    singbox = shutil.which("sing-box") or "/usr/bin/sing-box"
    LOCAL_SINGBOX_SERVICE.write_text(
        "[Unit]\nDescription=sing-box service managed by VPS Proxy Manager\n"
        "After=network-online.target nss-lookup.target\nWants=network-online.target\n"
        "[Service]\nUser=root\nCapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
        "AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW\n"
        f"ExecStart={singbox} run -c /etc/sing-box/config.json\n"
        "ExecReload=/bin/kill -HUP $MAINPID\nRestart=on-failure\nRestartSec=10\nLimitNOFILE=infinity\n"
        "[Install]\nWantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"], check=False)


def persist_agent() -> None:
    source = str(globals().get("_PAYLOAD_SOURCE", ""))
    if not source:
        if AGENT_SOURCE.exists():
            return
        fail("agent_source_missing", "初始化数据缺少远端 Agent 源码")
    AGENT_SOURCE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    atomic_write(AGENT_SOURCE, source, 0o700)
    atomic_write(
        AGENT_BIN,
        '#!/usr/bin/env bash\nset -euo pipefail\nexec python3 /usr/local/lib/vpspm-agent/agent.py "$@"\n',
        0o700,
    )


def initialize() -> None:
    require_supported_system()
    ensure_layout()
    backup = original_backup()
    if backup is None:
        backup = backup_state()
        atomic_write(BASE / "original-backup", str(backup) + "\n", 0o600)
    install_singbox()
    ensure_tun()
    write_service()
    persist_agent()
    run(["systemctl", "disable", "--now", "sing-box.service"], check=False, timeout=60)
    version = run(["sing-box", "version"], check=False).stdout.splitlines()
    ok(
        {
            "initialized": True,
            "agent_version": AGENT_VERSION,
            "singbox_version": version[0] if version else "unknown",
            "backup": str(backup),
            "system": detect_system(),
        }
    )


def upgrade_agent() -> None:
    require_supported_system()
    ensure_layout()
    persist_agent()
    ok({"upgraded": True, "agent_version": AGENT_VERSION})


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def config_sha256() -> str | None:
    if not SINGBOX_CONFIG.is_file() or SINGBOX_CONFIG.is_symlink():
        return None
    return hashlib.sha256(SINGBOX_CONFIG.read_bytes()).hexdigest()


def read_marker(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_selection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("bad_request", "缺少代理资源标识")
    kind = str(value.get("kind") or "")
    if kind not in {"node", "subscription"}:
        fail("bad_request", "代理资源类型无效")
    try:
        resource_id = int(value.get("resource_id"))
    except (TypeError, ValueError):
        fail("bad_request", "代理资源 ID 无效")
    fingerprint = str(value.get("fingerprint") or "")
    if resource_id < 1 or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        fail("bad_request", "代理资源标识无效")
    return {"kind": kind, "resource_id": resource_id, "fingerprint": fingerprint}


def stage_config(selection: dict[str, Any] | None = None) -> dict[str, Any]:
    digest = config_sha256()
    if digest is None:
        fail("proxy_config_missing", "没有可加载的代理配置")
    if selection is None:
        current = read_marker(ACTIVE_CONFIG)
        raw_selection = current.get("selection")
        selection = validate_selection(raw_selection) if raw_selection else None
    marker = {
        "config_sha256": digest,
        "selection": selection,
        "staged_at": int(time.time()),
    }
    atomic_write(PENDING_CONFIG, json.dumps(marker, ensure_ascii=False), 0o600)
    return marker


def promote_staged_config() -> dict[str, Any]:
    pending = read_marker(PENDING_CONFIG)
    digest = config_sha256()
    if not pending or digest is None or pending.get("config_sha256") != digest:
        fail("active_config_mismatch", "sing-box 已启动，但加载配置标识不一致")
    active = {
        "config_sha256": digest,
        "selection": pending.get("selection"),
        "activated_at": int(time.time()),
    }
    atomic_write(ACTIVE_CONFIG, json.dumps(active, ensure_ascii=False), 0o600)
    PENDING_CONFIG.unlink(missing_ok=True)
    return active


def library_id(data: dict[str, Any]) -> str:
    try:
        value = int(data.get("library_id"))
    except (TypeError, ValueError):
        fail("bad_request", "资源 ID 无效")
    if value <= 0:
        fail("bad_request", "资源 ID 无效")
    return str(value)


def store_node() -> None:
    data = read_input()
    item_id = library_id(data)
    link = str(data.get("link") or "")
    if not link or len(link.encode("utf-8")) > 65536:
        fail("bad_request", "节点内容为空或过大")
    record = {
        "id": int(item_id),
        "name": str(data.get("name") or "node")[:160],
        "link": link,
        "updated_at": int(time.time()),
    }
    ensure_layout()
    atomic_write(NODE_LIBRARY / f"{item_id}.json", json.dumps(record, ensure_ascii=False), 0o600)
    ok({"stored": True, "kind": "node", "library_id": int(item_id)})


def store_subscription() -> None:
    data = read_input()
    item_id = library_id(data)
    url = str(data.get("url") or "")
    if not url.startswith("https://") or len(url) > 4096:
        fail("bad_request", "订阅 URL 必须使用 HTTPS")
    record = {
        "id": int(item_id),
        "name": str(data.get("name") or "subscription")[:100],
        "url": url,
        "updated_at": int(time.time()),
    }
    ensure_layout()
    atomic_write(
        SUBSCRIPTION_LIBRARY / f"{item_id}.json",
        json.dumps(record, ensure_ascii=False),
        0o600,
    )
    ok({"stored": True, "kind": "subscription", "library_id": int(item_id)})


def remove_node() -> None:
    item_id = library_id(read_input())
    (NODE_LIBRARY / f"{item_id}.json").unlink(missing_ok=True)
    ok({"removed": True, "kind": "node", "library_id": int(item_id)})


def remove_subscription() -> None:
    item_id = library_id(read_input())
    (SUBSCRIPTION_LIBRARY / f"{item_id}.json").unlink(missing_ok=True)
    ok({"removed": True, "kind": "subscription", "library_id": int(item_id)})


def _validate_public_https_url(url: str) -> tuple[str, str, int, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        fail("subscription_url_invalid", "订阅 URL 必须是无内嵌凭据的 HTTPS 地址")
    hostname = str(parsed.hostname)
    port = parsed.port or 443
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        fail("subscription_dns_failed", "订阅域名解析失败", str(exc))
    public_ips: list[str] = []
    for result in addresses:
        ip_text = str(result[4][0])
        ip = ipaddress.ip_address(ip_text)
        if not ip.is_global:
            fail("subscription_ssrf_blocked", "订阅地址解析到了本地、私有或保留地址")
        if ip_text not in public_ips:
            public_ips.append(ip_text)
    return hostname, parsed.geturl(), port, public_ips[0]


def secure_fetch(url: str, *, timeout: int, max_bytes: int, max_redirects: int) -> bytes:
    current = url
    for _ in range(max_redirects + 1):
        hostname, validated_url, port, resolved_ip = _validate_public_https_url(current)
        temp_dir = Path(tempfile.mkdtemp(prefix="vpspm-subscription-"))
        headers_path = temp_dir / "headers"
        body_path = temp_dir / "body"
        result = run(
            [
                "curl",
                "-sS",
                "--noproxy",
                "*",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-redirs",
                "0",
                "--connect-timeout",
                str(min(timeout, 15)),
                "--max-time",
                str(timeout),
                "--max-filesize",
                str(max_bytes),
                "--resolve",
                f"{hostname}:{port}:{resolved_ip}",
                "-A",
                f"vpspm-agent/{AGENT_VERSION}",
                "-D",
                str(headers_path),
                "-o",
                str(body_path),
                "-w",
                "%{http_code}",
                validated_url,
            ],
            check=False,
            timeout=timeout + 5,
        )
        try:
            try:
                status_code = int(result.stdout.strip()[-3:])
            except ValueError:
                fail(
                    "subscription_http_error",
                    "订阅服务器没有返回有效 HTTP 状态",
                    result.stderr[-500:],
                )
            headers = headers_path.read_text(encoding="iso-8859-1") if headers_path.exists() else ""
            if status_code in {301, 302, 303, 307, 308}:
                location = ""
                for line in headers.splitlines():
                    if line.lower().startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                if not location:
                    fail("subscription_redirect_invalid", "订阅重定向缺少目标地址")
                current = urllib.parse.urljoin(current, location)
                continue
            if result.returncode != 0 or status_code < 200 or status_code >= 300:
                fail(
                    "subscription_http_error",
                    f"订阅服务器返回 HTTP {status_code}",
                    result.stderr[-500:],
                )
            if not body_path.exists():
                fail("subscription_empty_response", "订阅服务器返回空响应")
            if body_path.stat().st_size > max_bytes:
                fail("subscription_too_large", "订阅响应超过大小限制")
            body = body_path.read_bytes()
            if len(body) > max_bytes:
                fail("subscription_too_large", "订阅响应超过大小限制")
            return body
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    fail("subscription_redirect_limit", "订阅重定向次数过多")
    raise AssertionError("unreachable")


def fetch_subscription() -> None:
    data = read_input()
    item_id = library_id(data)
    path = SUBSCRIPTION_LIBRARY / f"{item_id}.json"
    if not path.exists():
        fail("subscription_not_found", "目标 VPS 中没有该订阅")
    record = json.loads(path.read_text(encoding="utf-8"))
    body = secure_fetch(
        str(record["url"]),
        timeout=max(3, min(int(data.get("timeout") or 12), 60)),
        max_bytes=max(1024, min(int(data.get("max_bytes") or 1048576), 4194304)),
        max_redirects=max(0, min(int(data.get("max_redirects") or 3), 5)),
    )
    ok(
        {
            "content_b64": base64.b64encode(body).decode("ascii"),
            "bytes": len(body),
            "library_id": int(item_id),
        }
    )


def write_rollback(backup: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
systemctl disable --now vpspm-rollback.timer 2>/dev/null || true
systemctl disable --now vpspm-activate.timer 2>/dev/null || true
systemctl stop sing-box.service 2>/dev/null || true
if [ -f "{backup}/config.json" ]; then
  install -d -m 755 /etc/sing-box
  install -m 600 "{backup}/config.json" /etc/sing-box/config.json
else
  rm -f /etc/sing-box/config.json
fi
if [ -f "{backup}/sing-box.service" ]; then
  install -m 644 "{backup}/sing-box.service" /etc/systemd/system/sing-box.service
else
  rm -f /etc/systemd/system/sing-box.service
fi
if [ -f "{backup}/active-config.json" ]; then
  install -m 600 "{backup}/active-config.json" "{ACTIVE_CONFIG}"
else
  rm -f "{ACTIVE_CONFIG}"
fi
rm -f "{PENDING_CONFIG}"
systemctl daemon-reload 2>/dev/null || true
if [ -f "{backup}/service-enabled" ]; then
  systemctl enable sing-box.service 2>/dev/null || true
else
  systemctl disable sing-box.service 2>/dev/null || true
fi
if [ -f "{backup}/service-active" ]; then
  systemctl start sing-box.service 2>/dev/null || true
else
  systemctl stop sing-box.service 2>/dev/null || true
fi
"""
    atomic_write(ROLLBACK_SCRIPT, script, 0o700)


def service_snapshot() -> dict[str, Any]:
    result = run(
        [
            "systemctl",
            "show",
            "sing-box.service",
            "--property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts",
        ],
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def recent_service_error() -> str:
    journal = run(
        [
            "journalctl",
            "-u",
            "sing-box.service",
            "--since",
            "1 minute ago",
            "-n",
            "40",
            "--no-pager",
            "-o",
            "cat",
        ],
        check=False,
    )
    lines = [
        line.strip()
        for line in journal.stdout.splitlines()
        if "FATAL" in line.upper() or "ERROR" in line.upper()
    ]
    text = "\n".join(lines[-3:])[-1200:]
    text = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "[uuid-redacted]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r'(?i)(password|private_key|public_key|uuid)(["\s:=]+)([^,}\s]+)',
        r"\1\2[redacted]",
        text,
    )


def wait_for_stable_service(*, timeout: float = 6.0, stable_for: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    active_since: float | None = None
    while time.monotonic() < deadline:
        active = (
            run(["systemctl", "is-active", "--quiet", "sing-box.service"], check=False).returncode
            == 0
        )
        now = time.monotonic()
        if active:
            active_since = active_since or now
            if now - active_since >= stable_for:
                return True
        else:
            active_since = None
        time.sleep(0.25)
    return False


def disarm_activation() -> None:
    run(["systemctl", "disable", "--now", "vpspm-activate.timer"], check=False)


def schedule_activation() -> None:
    disarm_activation()
    atomic_write(
        ACTIVATION_SERVICE,
        "[Unit]\nDescription=VPS Proxy Manager delayed proxy activation\n"
        "After=network-online.target\nWants=network-online.target\n"
        "[Service]\nType=oneshot\nExecStart=/usr/local/sbin/vpspm-agent activate_proxy\n",
        0o644,
    )
    atomic_write(
        ACTIVATION_TIMER,
        "[Unit]\nDescription=VPS Proxy Manager delayed proxy activation timer\n"
        "[Timer]\nOnActiveSec=2\nAccuracySec=100ms\nUnit=vpspm-activate.service\n"
        "[Install]\nWantedBy=timers.target\n",
        0o644,
    )
    atomic_write(
        ACTIVATION_STATUS,
        json.dumps({"state": "pending", "detail": {}}, ensure_ascii=False),
        0o600,
    )
    run(["systemctl", "daemon-reload"], check=False)
    scheduled = run(
        ["systemctl", "enable", "--now", "vpspm-activate.timer"],
        check=False,
    )
    if scheduled.returncode != 0:
        run([str(ROLLBACK_SCRIPT)], check=False)
        fail(
            "proxy_activation_schedule_failed",
            "代理延迟启动任务创建失败",
            scheduled.stderr[-800:],
        )


def activate_proxy() -> None:
    disarm_activation()
    started = run(["systemctl", "restart", "sing-box.service"], check=False, timeout=90)
    if started.returncode != 0 or not wait_for_stable_service():
        detail = service_snapshot()
        detail["error"] = recent_service_error()
        atomic_write(
            ACTIVATION_STATUS,
            json.dumps({"state": "failed", "detail": detail}, ensure_ascii=False),
            0o600,
        )
        run([str(ROLLBACK_SCRIPT)], check=False)
        fail("singbox_start_failed", "sing-box 启动后未能保持运行，已自动回滚")
    active_config = promote_staged_config()
    atomic_write(
        ACTIVATION_STATUS,
        json.dumps(
            {"state": "active", "detail": service_snapshot(), "active_config": active_config},
            ensure_ascii=False,
        ),
        0o600,
    )
    ok({"activated": True})


def arm_rollback(seconds: int) -> None:
    atomic_write(
        ROLLBACK_SERVICE,
        "[Unit]\nDescription=VPS Proxy Manager emergency rollback\n"
        "[Service]\nType=oneshot\nExecStart=/etc/vps-proxy-manager/rollback-last.sh\n",
        0o644,
    )
    atomic_write(
        ROLLBACK_TIMER,
        "[Unit]\nDescription=VPS Proxy Manager rollback timer\n"
        f"[Timer]\nOnActiveSec={seconds}\nUnit=vpspm-rollback.service\n"
        "[Install]\nWantedBy=timers.target\n",
        0o644,
    )
    run(["systemctl", "daemon-reload"], check=False)
    run(["systemctl", "enable", "--now", "vpspm-rollback.timer"], check=False)


def disarm_rollback() -> None:
    run(["systemctl", "disable", "--now", "vpspm-rollback.timer"], check=False)


def add_proxy_server_bypass(config: dict[str, Any]) -> None:
    try:
        outbound = config["outbounds"][0]
        inbound = config["inbounds"][0]
        server = str(outbound["server"])
        server_port = int(outbound["server_port"])
        rules = config["route"]["rules"]
    except (KeyError, IndexError, TypeError, ValueError):
        fail("bad_request", "代理配置缺少服务器或路由信息")
    try:
        addresses = socket.getaddrinfo(server, server_port, type=socket.SOCK_STREAM)
    except OSError as exc:
        fail("proxy_server_dns_failed", "目标 VPS 无法解析代理服务器", str(exc))
    cidrs: list[str] = []
    for result in addresses:
        ip = ipaddress.ip_address(str(result[4][0]))
        cidr = f"{ip}/32" if ip.version == 4 else f"{ip}/128"
        if cidr not in cidrs:
            cidrs.append(cidr)
    if not cidrs or not isinstance(rules, list):
        fail("proxy_server_dns_failed", "代理服务器没有可用地址")
    rules.insert(2, {"ip_cidr": cidrs, "outbound": "direct"})
    route_excludes = inbound.setdefault("route_exclude_address", [])
    if not isinstance(route_excludes, list):
        fail("bad_request", "TUN 绕过地址配置无效")
    for cidr in cidrs:
        if cidr not in route_excludes:
            route_excludes.append(cidr)


def apply_proxy() -> None:
    data = read_input()
    config = data.get("config")
    if not isinstance(config, dict):
        fail("bad_request", "缺少 sing-box 配置")
    selection = validate_selection(data.get("selection"))
    add_proxy_server_bypass(config)
    rollback_seconds = max(60, min(int(data.get("rollback_seconds") or 120), 600))
    ensure_layout()
    ensure_tun()
    backup = backup_state()
    write_rollback(backup)
    install_singbox()
    write_service()
    fd, name = tempfile.mkstemp(prefix="vpspm_singbox_", suffix=".json")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        check = run(["sing-box", "check", "-c", str(tmp)], check=False)
        if check.returncode != 0:
            fail("singbox_check_failed", "sing-box 配置校验失败", check.stderr[-1200:])
        shutil.copy2(tmp, SINGBOX_CONFIG)
        SINGBOX_CONFIG.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)
    staged = stage_config(selection)
    arm_rollback(rollback_seconds)
    enabled = run(["systemctl", "enable", "sing-box.service"], check=False, timeout=60)
    if enabled.returncode != 0:
        run([str(ROLLBACK_SCRIPT)], check=False)
        fail("singbox_enable_failed", "sing-box 开机启动配置失败，已自动回滚")
    schedule_activation()
    ok(
        {
            "backup": str(backup),
            "rollback_armed": True,
            "activation_scheduled": True,
            "config_sha256": staged["config_sha256"],
            "selection": selection,
            "agent_version": AGENT_VERSION,
        }
    )


def confirm_proxy() -> None:
    disarm_activation()
    disarm_rollback()
    ok({"rollback_armed": False})


def rollback() -> None:
    data = read_input()
    if not ROLLBACK_SCRIPT.exists():
        fail("no_backup", "没有可用回滚脚本")
    result = run([str(ROLLBACK_SCRIPT)], check=False)
    if result.returncode != 0:
        fail("rollback_failed", "回滚执行失败", result.stderr[-1200:])
    atomic_write(
        ACTIVATION_STATUS,
        json.dumps({"state": "rolled_back", "detail": {}}, ensure_ascii=False),
        0o600,
    )
    backup = Path((BASE / "last-backup").read_text(encoding="utf-8").strip())
    mode = "proxy" if (backup / "service-active").exists() else "local"
    active_config: dict[str, Any] = {}
    if mode == "proxy" and data.get("selection"):
        staged = stage_config(validate_selection(data.get("selection")))
        active_config = promote_staged_config()
        if active_config.get("config_sha256") != staged.get("config_sha256"):
            fail("rollback_config_mismatch", "回滚后的活动配置标识不一致")
    ok({"rolled_back": True, "exit_mode": mode, "active_config": active_config})


def stop_proxy() -> None:
    disarm_activation()
    disarm_rollback()
    run(["systemctl", "disable", "--now", "sing-box.service"], check=False)
    atomic_write(
        ACTIVATION_STATUS,
        json.dumps({"state": "stopped", "detail": {}}, ensure_ascii=False),
        0o600,
    )
    ok({"stopped": True, "exit_mode": "local", "persistent": True})


def restore_proxy() -> None:
    data = read_input()
    if not SINGBOX_CONFIG.exists():
        fail("proxy_config_missing", "没有可恢复的代理配置")
    backup = backup_state()
    write_rollback(backup)
    selection_value = data.get("selection")
    selection = validate_selection(selection_value) if selection_value else None
    staged = stage_config(selection)
    arm_rollback(120)
    enabled = run(["systemctl", "enable", "sing-box.service"], check=False)
    if enabled.returncode != 0:
        run([str(ROLLBACK_SCRIPT)], check=False)
        fail("restore_failed", "代理服务恢复配置失败，已自动回滚")
    schedule_activation()
    ok(
        {
            "running": True,
            "exit_mode": "proxy",
            "persistent": True,
            "rollback_armed": True,
            "activation_scheduled": True,
            "config_sha256": staged["config_sha256"],
        }
    )


def uninstall() -> None:
    safety_backup = backup_state()
    disarm_activation()
    run(["systemctl", "disable", "--now", "sing-box.service"], check=False)
    run(["systemctl", "disable", "--now", "vpspm-rollback.timer"], check=False)
    original = original_backup()
    if original is not None:
        restore_backup(original)
    else:
        SINGBOX_CONFIG.unlink(missing_ok=True)
        LOCAL_SINGBOX_SERVICE.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"], check=False)
    shutil.rmtree(LIBRARY, ignore_errors=True)
    PENDING_CONFIG.unlink(missing_ok=True)
    if original is None or not (original / "active-config.json").exists():
        ACTIVE_CONFIG.unlink(missing_ok=True)
    if original is not None and not (original / "singbox-preexisting").exists():
        run(["apt-get", "remove", "-y", "sing-box"], check=False, timeout=300)
    mode = "proxy" if original is not None and (original / "service-active").exists() else "local"
    atomic_write(
        ACTIVATION_STATUS,
        json.dumps({"state": "uninstalled", "detail": {}}, ensure_ascii=False),
        0o600,
    )
    ok(
        {
            "uninstalled": True,
            "exit_mode": mode,
            "original_restored": original is not None,
            "safety_backup": str(safety_backup),
        }
    )


def status() -> None:
    service = run(["systemctl", "is-active", "sing-box.service"], check=False)
    service_active = service.stdout.strip() == "active"
    current_config_sha256 = config_sha256()
    active_config = read_marker(ACTIVE_CONFIG)
    config_consistent = bool(
        service_active
        and current_config_sha256
        and active_config.get("config_sha256") == current_config_sha256
    )
    rollback_armed = (
        run(["systemctl", "is-active", "--quiet", "vpspm-rollback.timer"], check=False).returncode
        == 0
    )
    activation_status: dict[str, Any] = {}
    if ACTIVATION_STATUS.exists():
        try:
            loaded_status = json.loads(ACTIVATION_STATUS.read_text(encoding="utf-8"))
            if isinstance(loaded_status, dict):
                activation_status = loaded_status
        except (OSError, ValueError):
            activation_status = {"state": "invalid", "detail": {}}
    version_lines = (
        run(["sing-box", "version"], check=False).stdout.splitlines()
        if shutil.which("sing-box")
        else []
    )
    outbound = (
        run(
            [
                "curl",
                "--noproxy",
                "*",
                "-4fsS",
                "--max-time",
                "8",
                "https://ifconfig.co/json",
            ],
            check=False,
            timeout=12,
        )
        if shutil.which("curl")
        else None
    )
    connectivity = (
        run(
            [
                "curl",
                "--noproxy",
                "*",
                "-4fsS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--max-time",
                "8",
                "https://www.gstatic.com/generate_204",
            ],
            check=False,
            timeout=12,
        )
        if shutil.which("curl")
        else None
    )
    outbound_ok = bool(outbound and outbound.returncode == 0 and outbound.stdout.strip())
    generate_204_ok = bool(
        connectivity and connectivity.returncode == 0 and connectivity.stdout.strip() == "204"
    )
    node_count = len(list(NODE_LIBRARY.glob("*.json"))) if NODE_LIBRARY.exists() else 0
    sub_count = (
        len(list(SUBSCRIPTION_LIBRARY.glob("*.json"))) if SUBSCRIPTION_LIBRARY.exists() else 0
    )
    ok(
        {
            "status": {
                "agent_version": AGENT_VERSION if AGENT_SOURCE.exists() else None,
                "singbox_active": service.stdout.strip(),
                "singbox_service": service_snapshot(),
                "config_sha256": current_config_sha256,
                "active_config": active_config,
                "config_consistent": config_consistent,
                "rollback_armed": rollback_armed,
                "activation_status": activation_status,
                "singbox_version": version_lines[0] if version_lines else "",
                "dns_mode": "tun_hijack_to_proxy" if service_active else "system_local",
                "outbound_probe": outbound.stdout[:800]
                if outbound and outbound.returncode == 0
                else "",
                "outbound_error": outbound.stderr[:400]
                if outbound and outbound.returncode != 0
                else "",
                "connectivity_ok": outbound_ok or generate_204_ok,
                "connectivity_probes": {
                    "outbound_ip": outbound_ok,
                    "generate_204": generate_204_ok,
                },
                "has_backup": (BASE / "last-backup").exists(),
                "has_original_backup": original_backup() is not None,
                "managed_library_exists": LIBRARY.exists(),
                "node_count": node_count,
                "subscription_count": sub_count,
            }
        }
    )


def speedtest() -> None:
    data = read_input()
    config = data.get("config")
    port = int(data.get("listen_port") or 18080)
    attempts = max(1, min(int(data.get("attempts") or 3), 5))
    if not isinstance(config, dict):
        fail("bad_request", "缺少测速配置")
    install_singbox()
    server = str(config["outbounds"][0]["server"])
    server_port = int(config["outbounds"][0]["server_port"])
    dns_started = time.monotonic()
    dns_ok = False
    tcp_ok = False
    dns_ms: int | None = None
    tcp_ms: int | None = None
    error = ""
    try:
        addr = socket.getaddrinfo(server, server_port, type=socket.SOCK_STREAM)[0][4][0]
        dns_ms = int((time.monotonic() - dns_started) * 1000)
        dns_ok = True
        tcp_started = time.monotonic()
        with socket.create_connection((addr, server_port), timeout=5):
            tcp_ok = True
            tcp_ms = int((time.monotonic() - tcp_started) * 1000)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    fd, name = tempfile.mkstemp(prefix="vpspm_speed_", suffix=".json")
    os.close(fd)
    cfg = Path(name)
    cfg.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen(
        ["sing-box", "run", "-c", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    samples: list[dict[str, int]] = []
    curl_error = ""
    try:
        time.sleep(1.2)
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            fail("proxy_start_failed", "测速代理实例启动失败", stderr[-1200:])
        for _ in range(attempts):
            curl = run(
                [
                    "curl",
                    "--noproxy",
                    "",
                    "-x",
                    f"http://127.0.0.1:{port}",
                    "-fsS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{time_starttransfer} %{time_total}",
                    "--max-time",
                    "15",
                    "https://www.gstatic.com/generate_204",
                ],
                check=False,
                timeout=20,
            )
            if curl.returncode != 0:
                curl_error = curl.stderr.strip()
                continue
            try:
                handshake_s, total_s = curl.stdout.strip().split()
                samples.append(
                    {
                        "proxy_handshake_ms": int(float(handshake_s) * 1000),
                        "access_latency_ms": int(float(total_s) * 1000),
                    }
                )
            except (ValueError, TypeError):
                curl_error = "curl timing output invalid"
        proxy_ok = bool(samples)
        average_handshake = (
            int(sum(x["proxy_handshake_ms"] for x in samples) / len(samples)) if samples else None
        )
        average_access = (
            int(sum(x["access_latency_ms"] for x in samples) / len(samples)) if samples else None
        )
        exit_info: dict[str, Any] = {}
        if proxy_ok:
            exit_probe = run(
                [
                    "curl",
                    "--noproxy",
                    "",
                    "-x",
                    f"http://127.0.0.1:{port}",
                    "-4fsS",
                    "--max-time",
                    "15",
                    "https://ifconfig.co/json",
                ],
                check=False,
                timeout=20,
            )
            if exit_probe.returncode == 0:
                try:
                    exit_data = json.loads(exit_probe.stdout[:2000])
                    if isinstance(exit_data, dict):
                        exit_info = {
                            "exit_ip": exit_data.get("ip"),
                            "exit_country": exit_data.get("country"),
                            "exit_country_iso": exit_data.get("country_iso"),
                        }
                except ValueError:
                    pass
        ok(
            {
                "result": {
                    "dns_ok": dns_ok,
                    "dns_latency_ms": dns_ms,
                    "tcp_ok": tcp_ok,
                    "tcp_latency_ms": tcp_ms,
                    "proxy_ok": proxy_ok,
                    "proxy_handshake_ms": average_handshake,
                    "access_latency_ms": average_access,
                    "latency_ms": average_access,
                    "attempts": attempts,
                    "successful_attempts": len(samples),
                    "samples": samples,
                    **exit_info,
                    "test_url": "https://www.gstatic.com/generate_204",
                    "error": "" if proxy_ok else (curl_error or error or "proxy test failed"),
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
        "initialize": initialize,
        "upgrade_agent": upgrade_agent,
        "activate_proxy": activate_proxy,
        "store_node": store_node,
        "store_subscription": store_subscription,
        "remove_node": remove_node,
        "remove_subscription": remove_subscription,
        "fetch_subscription": fetch_subscription,
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
