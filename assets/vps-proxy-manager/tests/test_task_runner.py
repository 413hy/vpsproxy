from __future__ import annotations

from typing import Any

import pytest

from tests.test_parser import VLESS_REALITY
from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.models import (
    AuthMethod,
    Base,
    ProxyMode,
    Task,
    TaskKind,
    TaskStatus,
)
from vps_proxy_manager.proxy.parser import parse_node_link
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.tasks.runner import TaskRunner


class FakeSSH:
    def __init__(self, *, connectivity_ok: bool = True) -> None:
        self.connectivity_ok = connectivity_ok
        self.actions: list[str] = []

    async def run_agent(
        self, _creds: object, action: str, _data: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        self.actions.append(action)
        if action == "detect":
            return {
                "system": {
                    "ssh_client_ip": "198.51.100.11",
                    "os_release": {"ID": "debian"},
                }
            }
        if action == "apply_proxy":
            return {"backup": "/safe/backup", "rollback_armed": True}
        if action == "status":
            return {
                "status": {
                    "singbox_active": "active",
                    "connectivity_ok": self.connectivity_ok,
                }
            }
        if action == "confirm_proxy":
            return {"rollback_armed": False}
        raise AssertionError(f"unexpected action: {action}")


def settings(secret_key: str) -> Settings:
    return Settings(
        telegram_bot_token="test-token-placeholder-value",  # noqa: S106
        admin_user_ids=[1],
        secret_key=secret_key,
    )


async def create_apply_task(
    connectivity_ok: bool,
) -> tuple[Any, Any, FakeSSH, int, int, TaskRunner]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    key = generate_key()
    secret_box = SecretBox(key)
    async with factory() as session:
        repo = Repository(session, secret_box)
        node = (await repo.save_nodes([parse_node_link(VLESS_REALITY)]))[0]
        host = await repo.add_host(
            name="target",
            host="203.0.113.20",
            port=22,
            username="root",
            auth_method=AuthMethod.password,
            secret="test-only",  # noqa: S106
            known_host="known-host",
            system_info={"ssh_client_ip": "198.51.100.10"},
        )
        assigned = await repo.assign_node(host, node)
        task = await repo.create_task(
            kind=TaskKind.apply_proxy,
            actor_user_id=1,
            host_id=host.id,
            payload={"vps_node_id": assigned.id},
        )
        await session.commit()
        task_id = task.id
        host_id = host.id
    ssh = FakeSSH(connectivity_ok=connectivity_ok)
    runner = TaskRunner(factory, settings(key), secret_box, ssh_client=ssh)
    return engine, factory, ssh, task_id, host_id, runner


@pytest.mark.asyncio
async def test_apply_proxy_confirms_only_after_remote_connectivity_verification() -> None:
    engine, factory, ssh, task_id, host_id, runner = await create_apply_task(True)

    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        state = await Repository(session, runner.secret_box).get_proxy_state(host_id)
        assert task is not None and task.status == TaskStatus.succeeded
        assert state.mode == ProxyMode.proxy
        assert task.result["previous_state"]["mode"] == ProxyMode.local.value
    assert ssh.actions == ["detect", "apply_proxy", "status", "confirm_proxy"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_proxy_leaves_rollback_armed_when_connectivity_fails() -> None:
    engine, factory, ssh, task_id, host_id, runner = await create_apply_task(False)

    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        state = await Repository(session, runner.secret_box).get_proxy_state(host_id)
        assert task is not None and task.status == TaskStatus.failed
        assert task.error_code == "proxy_verification_failed"
        assert state.mode == ProxyMode.local
    assert ssh.actions == ["detect", "apply_proxy", "status"]
    await engine.dispose()
