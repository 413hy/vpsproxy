from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from vps_proxy_manager.config import get_settings
from vps_proxy_manager.crypto import generate_key


def test_legacy_subscription_nodes_migrate_to_separate_entries(tmp_path: Path, monkeypatch) -> None:
    project_dir = Path(__file__).resolve().parents[1]
    database = tmp_path / "legacy.db"
    monkeypatch.setenv("VPSPM_TELEGRAM_BOT_TOKEN", "test-token-placeholder-value")
    monkeypatch.setenv("VPSPM_ADMIN_USER_IDS", "1")
    monkeypatch.setenv("VPSPM_SECRET_KEY", generate_key())
    monkeypatch.setenv("VPSPM_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("VPSPM_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    config = Config(str(project_dir / "alembic.ini"))
    config.set_main_option("script_location", str(project_dir / "migrations"))
    command.upgrade(config, "0001_initial")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO subscriptions (
                id, name, encrypted_url, enabled, update_interval_hours,
                last_update_at, last_error, created_at
            ) VALUES (1, 'legacy', 'encrypted-url', 1, NULL, NULL, NULL, CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO proxy_nodes (
                id, name, protocol, server, port, subscription_id, encrypted_link,
                fingerprint, tags, status, last_latency_ms, last_test, created_at, updated_at
            ) VALUES (
                1, 'legacy-entry', 'vless', 'example.com', 443, 1, 'encrypted-link',
                'fingerprint', '[]', 'online', 100, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        entry = connection.execute(
            "SELECT subscription_id, name, last_latency_ms FROM subscription_entries"
        ).fetchone()
        count = connection.execute("SELECT node_count FROM subscriptions WHERE id = 1").fetchone()
        codex_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(codex_tasks)").fetchall()
        }
    assert entry == (1, "legacy-entry", 100)
    assert count == (1,)
    assert codex_columns["candidate_id"][3] == 0
    assert "source_task_id" in codex_columns
    get_settings.cache_clear()
