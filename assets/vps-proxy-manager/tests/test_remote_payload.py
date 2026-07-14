from __future__ import annotations

from pathlib import Path

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
