from __future__ import annotations

from pathlib import Path
from typing import Any

from vps_proxy_manager.remote import payload


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


def test_stop_proxy_disables_service_persistently(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "run", fake_run)
    payload.stop_proxy()
    assert ["systemctl", "disable", "--now", "sing-box.service"] in calls
    assert ["systemctl", "disable", "--now", "vpspm-rollback.timer"] in calls


def test_restore_proxy_enables_service_persistently(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(payload, "run", fake_run)
    payload.restore_proxy()
    assert ["systemctl", "enable", "--now", "sing-box.service"] in calls
