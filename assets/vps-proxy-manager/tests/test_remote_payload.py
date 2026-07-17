from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from subprocess import CompletedProcess
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


def test_stop_proxy_disables_service_persistently(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "run", fake_run)
    monkeypatch.setattr(payload, "ACTIVATION_STATUS", tmp_path / "activation.json")
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
    scheduled: list[bool] = []
    monkeypatch.setattr(payload, "schedule_activation", lambda: scheduled.append(True))
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(payload, "SINGBOX_CONFIG", config)
    monkeypatch.setattr(payload, "ACTIVE_CONFIG", tmp_path / "active.json")
    monkeypatch.setattr(payload, "PENDING_CONFIG", tmp_path / "pending.json")
    monkeypatch.setattr(payload, "read_input", lambda: {})
    payload.restore_proxy()
    assert ["systemctl", "enable", "sing-box.service"] in calls
    assert rollback_calls == [backup]
    assert armed == [120]
    assert scheduled == [True]


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
    monkeypatch.setattr(payload, "ACTIVE_CONFIG", base / "active.json")
    monkeypatch.setattr(payload, "PENDING_CONFIG", base / "pending.json")
    monkeypatch.setattr(payload, "run", fake_run)
    payload.restore_backup(backup)

    assert config.read_text() == '{"restored": true}'
    assert ["systemctl", "stop", "sing-box.service"] in calls
    assert ["systemctl", "start", "sing-box.service"] not in calls


def test_apply_proxy_schedules_activation_before_network_change(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    base = tmp_path / "base"
    config_path = tmp_path / "sing-box" / "config.json"
    rollback_script = base / "rollback-last.sh"
    rollback_script.parent.mkdir(parents=True)
    rollback_script.write_text("#!/bin/sh\n")
    backup = tmp_path / "backup"
    backup.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(payload, "BASE", base)
    monkeypatch.setattr(payload, "SINGBOX_CONFIG", config_path)
    monkeypatch.setattr(payload, "ROLLBACK_SCRIPT", rollback_script)
    monkeypatch.setattr(payload, "PENDING_CONFIG", base / "pending.json")
    monkeypatch.setattr(payload, "ACTIVE_CONFIG", base / "active.json")
    monkeypatch.setattr(
        payload,
        "read_input",
        lambda: {
            "config": {"outbounds": [], "route": {}},
            "selection": {"kind": "node", "resource_id": 1, "fingerprint": "a" * 64},
        },
    )
    monkeypatch.setattr(payload, "add_proxy_server_bypass", lambda _config: None)
    monkeypatch.setattr(payload, "ensure_layout", lambda: config_path.parent.mkdir(parents=True))
    monkeypatch.setattr(payload, "ensure_tun", lambda: None)
    monkeypatch.setattr(payload, "backup_state", lambda: backup)
    monkeypatch.setattr(payload, "write_rollback", lambda _backup: None)
    monkeypatch.setattr(payload, "install_singbox", lambda: None)
    monkeypatch.setattr(payload, "write_service", lambda: None)
    monkeypatch.setattr(payload, "arm_rollback", lambda _seconds: None)
    scheduled: list[bool] = []
    monkeypatch.setattr(payload, "schedule_activation", lambda: scheduled.append(True))
    monkeypatch.setattr(payload, "run", fake_run)

    payload.apply_proxy()

    response = capsys.readouterr().out
    assert '"activation_scheduled": true' in response
    assert ["systemctl", "enable", "sing-box.service"] in calls
    assert ["systemctl", "enable", "--now", "sing-box.service"] not in calls
    assert scheduled == [True]


def test_delayed_activation_rolls_back_when_service_does_not_stabilize(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    status_path = tmp_path / "activation.json"
    rollback_script = tmp_path / "rollback.sh"
    rollback_script.write_text("#!/bin/sh\n")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(payload, "ACTIVATION_STATUS", status_path)
    monkeypatch.setattr(payload, "ROLLBACK_SCRIPT", rollback_script)
    monkeypatch.setattr(payload, "disarm_activation", lambda: None)
    monkeypatch.setattr(payload, "wait_for_stable_service", lambda: False)
    monkeypatch.setattr(payload, "service_snapshot", lambda: {"Result": "exit-code"})
    monkeypatch.setattr(payload, "run", fake_run)

    with pytest.raises(SystemExit):
        payload.activate_proxy()

    response = capsys.readouterr().out
    assert '"code": "singbox_start_failed"' in response
    assert [str(rollback_script)] in calls
    assert ["systemctl", "restart", "sing-box.service"] in calls
    assert '"state": "failed"' in status_path.read_text()


def test_delayed_activation_restarts_and_promotes_exact_config(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"route": {}}')
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    pending = tmp_path / "pending.json"
    active = tmp_path / "active.json"
    status_path = tmp_path / "activation.json"
    selection = {"kind": "node", "resource_id": 4, "fingerprint": "b" * 64}
    pending.write_text(json.dumps({"config_sha256": digest, "selection": selection}))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(payload, "SINGBOX_CONFIG", config)
    monkeypatch.setattr(payload, "PENDING_CONFIG", pending)
    monkeypatch.setattr(payload, "ACTIVE_CONFIG", active)
    monkeypatch.setattr(payload, "ACTIVATION_STATUS", status_path)
    monkeypatch.setattr(payload, "disarm_activation", lambda: None)
    monkeypatch.setattr(payload, "wait_for_stable_service", lambda: True)
    monkeypatch.setattr(payload, "service_snapshot", lambda: {"ActiveState": "active"})
    monkeypatch.setattr(payload, "run", fake_run)

    payload.activate_proxy()

    marker = json.loads(active.read_text())
    assert ["systemctl", "restart", "sing-box.service"] in calls
    assert marker["config_sha256"] == digest
    assert marker["selection"] == selection
    assert not pending.exists()
    assert '"activated": true' in capsys.readouterr().out


def test_recent_service_error_redacts_credentials(monkeypatch: Any) -> None:
    raw = (
        "FATAL uuid=11111111-1111-4111-8111-111111111111 "
        'password="do-not-log" configuration failed\n'
    )

    def fake_run(_argv: list[str], **_: object) -> CompletedProcess[str]:
        return CompletedProcess([], 0, stdout=raw, stderr="")

    monkeypatch.setattr(payload, "run", fake_run)
    result = payload.recent_service_error()
    assert "11111111" not in result
    assert "do-not-log" not in result


def test_target_adds_resolved_proxy_server_bypass(monkeypatch: Any) -> None:
    config = {
        "inbounds": [{"type": "tun", "route_exclude_address": []}],
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
    assert "203.0.113.10/32" in config["inbounds"][0]["route_exclude_address"]
