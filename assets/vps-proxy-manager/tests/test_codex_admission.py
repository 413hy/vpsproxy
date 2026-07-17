from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from vps_proxy_manager.codex.worker import CodexWorker, provision_candidate
from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.models import (
    AuthMethod,
    Base,
    CodexTaskStatus,
    HostLifecycle,
    Task,
    TaskKind,
    TaskStatus,
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


@pytest.mark.asyncio
async def test_codex_diagnosis_context_and_result_are_linked_to_failed_task() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    key = generate_key()
    secret_box = SecretBox(key)
    async with factory() as session:
        repo = Repository(session, secret_box)
        source = await repo.create_task(
            kind=TaskKind.vps_subscription_test,
            actor_user_id=1,
            payload={"vps_subscription_id": 7},
        )
        source.status = TaskStatus.failed
        source.error_code = "internal_error"
        source.message = "任务失败"
        source.result = {
            "technical_error": "database is locked",
            "exception_type": "OperationalError",
        }
        diagnostic = await repo.create_codex_diagnostic(source.id)
        diagnostic.status = CodexTaskStatus.running
        await session.commit()
        source_id, diagnostic_id = source.id, diagnostic.id
    worker = CodexWorker(
        factory,
        Settings(
            telegram_bot_token="test-token-placeholder-value",  # noqa: S106
            admin_user_ids=[1],
            secret_key=key,
        ),
        secret_box,
    )
    worker._notify_diagnosis = AsyncMock()  # type: ignore[method-assign]

    context = await worker._diagnostic_context(diagnostic_id)
    assert context["source_task"]["technical_result"]["technical_error"] == "database is locked"
    assert "credentials" not in str(context).lower()

    await worker._complete_diagnosis(
        diagnostic_id,
        {
            "severity": "error",
            "summary": "SQLite 写事务冲突",
            "root_cause": "长事务与进度更新争用写锁",
            "evidence": ["database is locked"],
            "recommended_actions": ["提交条目后再测速"],
            "retry_safe": True,
        },
    )

    async with factory() as session:
        repo = Repository(session, secret_box)
        diagnostic = await repo.get_codex_task(diagnostic_id)
        source = await session.get(Task, source_id)
        assert diagnostic.status == CodexTaskStatus.succeeded
        assert diagnostic.result["model"] == "gpt-5.6-sol"
        assert source is not None
        assert source.result["codex_diagnostic"]["retry_safe"] is True
    await engine.dispose()
