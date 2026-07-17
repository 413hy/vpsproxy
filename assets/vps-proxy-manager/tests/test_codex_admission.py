from __future__ import annotations

from typing import Any

import pytest

from vps_proxy_manager.codex.worker import provision_candidate
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.models import (
    AuthMethod,
    Base,
    CodexTaskStatus,
    HostLifecycle,
)
from vps_proxy_manager.services.repository import Repository


class AdmissionSSH:
    async def run_payload(
        self, _creds: object, action: str, _data: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        assert action == "initialize"
        return {
            "agent_version": "0.2.0",
            "system": {"os_release": {"ID": "debian"}, "tun_available": True},
        }

    async def run_agent(
        self, _creds: object, action: str, _data: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        assert action == "status"
        return {
            "status": {
                "agent_version": "0.2.0",
                "singbox_version": "sing-box version 1.13.0",
                "singbox_active": "inactive",
                "connectivity_ok": True,
                "has_backup": True,
            }
        }


@pytest.mark.asyncio
async def test_candidate_enters_vps_inventory_only_after_codex_admission() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    secret_box = SecretBox(generate_key())
    async with factory() as session:
        repo = Repository(session, secret_box)
        candidate = await repo.add_candidate(
            name="pending-target",
            host="203.0.113.30",
            port=22,
            username="root",
            auth_method=AuthMethod.password,
            secret="test-only",  # noqa: S106
            known_host="known-host",
            system_info={},
        )
        task = await repo.create_codex_task(candidate.id)
        task.status = CodexTaskStatus.running
        await session.commit()
        candidate_id, task_id = candidate.id, task.id
        assert await repo.list_hosts() == []

    result = await provision_candidate(
        factory,
        secret_box,
        AdmissionSSH(),
        candidate_id=candidate_id,
        codex_task_id=task_id,
    )

    async with factory() as session:
        repo = Repository(session, secret_box)
        hosts = await repo.list_hosts()
        candidate = await repo.get_candidate(candidate_id)
        task = await repo.get_codex_task(task_id)
        assert result["ok"] is True
        assert len(hosts) == 1
        assert candidate.lifecycle == HostLifecycle.ready
        assert task.status == CodexTaskStatus.succeeded
    await engine.dispose()
