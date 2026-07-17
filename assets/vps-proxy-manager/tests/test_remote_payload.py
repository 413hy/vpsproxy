from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from vps_proxy_manager.remote import payload


def test_remote_subscription_fetch_blocks_private_dns(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        ],
    )
    with pytest.raises(SystemExit):
        payload._validate_public_https_url("https://metadata.example/sub")


def test_rollback_script_contains_fixed_paths(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "base"
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setattr(payload, "BASE", base)
    monkeypatch.setattr(payload, "ROLLBACK_SCRIPT", base / "rollback-last.sh")
    payload.ensure_layout()
    payload.write_rollback(backup)
    text = (base / "rollback-last.sh").read_text()
    assert "systemctl stop sing-box.service" in text
    assert "rm -f /etc/sing-box/config.json" in text
    assert f'if [ -f "{backup}/service-active" ]' in text
    assert "systemctl restart sing-box.service" not in text


def test_stop_proxy_disables_service_persistently(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "run", fake_run)
    payload.stop_proxy()
    assert ["systemctl", "disable", "--now", "sing-box.service"] in calls
    assert ["systemctl", "disable", "--now", "vpspm-rollback.timer"] in calls


def test_restore_proxy_enables_service_persistently(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    rollback_calls: list[Path] = []
    armed: list[int] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "run", fake_run)
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setattr(payload, "backup_state", lambda: backup)
    monkeypatch.setattr(payload, "write_rollback", rollback_calls.append)
    monkeypatch.setattr(payload, "arm_rollback", armed.append)
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(payload, "SINGBOX_CONFIG", config)
    payload.restore_proxy()
    assert ["systemctl", "enable", "--now", "sing-box.service"] in calls
    assert rollback_calls == [backup]
    assert armed == [120]


def test_restore_backup_keeps_stopped_service_stopped(monkeypatch: Any, tmp_path: Path) -> None:
    base = tmp_path / "base"
    backups = base / "backups"
    backup = backups / "snapshot"
    backup.mkdir(parents=True)
    (backup / "config.json").write_text('{"restored": true}')
    config = tmp_path / "sing-box" / "config.json"
    service = tmp_path / "systemd" / "sing-box.service"
    service.parent.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "BASE", base)
    monkeypatch.setattr(payload, "BACKUPS", backups)
    monkeypatch.setattr(payload, "SINGBOX_CONFIG", config)
    monkeypatch.setattr(payload, "LOCAL_SINGBOX_SERVICE", service)
    monkeypatch.setattr(payload, "run", fake_run)
    payload.restore_backup(backup)

    assert config.read_text() == '{"restored": true}'
    assert ["systemctl", "stop", "sing-box.service"] in calls
    assert ["systemctl", "start", "sing-box.service"] not in calls


def test_target_adds_resolved_proxy_server_bypass(monkeypatch: Any) -> None:
    config = {
        "outbounds": [{"server": "proxy.example", "server_port": 443}],
        "route": {"rules": [{"action": "sniff"}, {"protocol": "dns"}]},
    }
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))
        ],
    )
    payload.add_proxy_server_bypass(config)
    assert config["route"]["rules"][2] == {
        "ip_cidr": ["203.0.113.10/32"],
        "outbound": "direct",
    }
