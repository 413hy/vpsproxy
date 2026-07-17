from __future__ import annotations

import base64
from typing import Any

import pytest
from sqlalchemy import select

from tests.test_parser import VLESS_REALITY
from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.models import (
    AuthMethod,
    Base,
    CodexTask,
    ProxyMode,
    Task,
    TaskKind,
    TaskStatus,
)
from vps_proxy_manager.proxy.parser import parse_node_link
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.tasks.runner import TaskRunner


class FakeSSH:
    def __init__(
        self,
        *,
        connectivity_ok: bool = True,
        config_consistent: bool = True,
        activation_states: list[str] | None = None,
    ) -> None:
        self.connectivity_ok = connectivity_ok
        self.config_consistent = config_consistent
        self.activation_states = list(activation_states or [])
        self.actions: list[str] = []
        self.selection: dict[str, Any] | None = None

    async def run_payload(
        self, _creds: object, action: str, _data: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        self.actions.append(action)
        assert action == "upgrade_agent"
        return {"agent_version": "0.3.2"}

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
            self.selection = _data["selection"]
            return {
                "backup": "/safe/backup",
                "rollback_armed": True,
                "config_sha256": "a" * 64,
            }
        if action == "status":
            activation_state = self.activation_states.pop(0) if self.activation_states else None
            return {
                "status": {
                    "singbox_active": "active",
                    "connectivity_ok": self.connectivity_ok,
                    "config_consistent": self.config_consistent,
                    "active_config": {
                        "config_sha256": "a" * 64,
                        "selection": self.selection,
                    },
                    **(
                        {"activation_status": {"state": activation_state}}
                        if activation_state
                        else {}
                    ),
                }
            }
        if action == "confirm_proxy":
            return {"rollback_armed": False}
        raise AssertionError(f"unexpected action: {action}")


class SubscriptionSSH:
    async def run_agent(
        self, _creds: object, action: str, _data: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        if action == "fetch_subscription":
            return {"content_b64": base64.b64encode(VLESS_REALITY.encode()).decode()}
        raise AssertionError(f"unexpected action: {action}")


class TransactionCheckingRunner(TaskRunner):
    async def _run_remote_tests(
        self,
        session: Any,
        _task: Task,
        _host: Any,
        items: Any,
    ) -> list[tuple[Any, dict[str, Any]]]:
        assert session.in_transaction() is False
        return [
            (
                item,
                {
                    "dns_ok": True,
                    "dns_latency_ms": 1,
                    "tcp_ok": True,
                    "tcp_latency_ms": 2,
                    "proxy_ok": True,
                    "proxy_handshake_ms": 3,
                    "access_latency_ms": 4,
                    "error": "",
                },
            )
            for item in items
        ]


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
        assert task.result["status"]["rollback_armed"] is False
    assert ssh.actions == ["upgrade_agent", "detect", "apply_proxy", "status", "confirm_proxy"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_proxy_waits_for_delayed_activation_to_stabilize(monkeypatch: Any) -> None:
    engine, factory, ssh, task_id, _host_id, runner = await create_apply_task(True)
    ssh.activation_states = ["pending", "active"]

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("vps_proxy_manager.tasks.runner.asyncio.sleep", no_wait)
    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == TaskStatus.succeeded
    assert ssh.actions.count("status") == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_proxy_leaves_rollback_armed_when_connectivity_fails() -> None:
    engine, factory, ssh, task_id, host_id, runner = await create_apply_task(False)

    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        state = await Repository(session, runner.secret_box).get_proxy_state(host_id)
        assert task is not None and task.status == TaskStatus.failed
        assert task.error_code == "proxy_connectivity_failed"
        diagnostic = await session.scalar(
            select(CodexTask).where(CodexTask.source_task_id == task.id)
        )
        assert diagnostic is not None and diagnostic.operation == "diagnose"
        assert task.result["codex_diagnostic_task_id"] == diagnostic.id
        assert state.mode == ProxyMode.local
    assert ssh.actions == ["upgrade_agent", "detect", "apply_proxy", "status"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_proxy_rejects_running_process_with_stale_config() -> None:
    engine, factory, ssh, task_id, _host_id, runner = await create_apply_task(True)
    ssh.config_consistent = False

    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None and task.status == TaskStatus.failed
        assert task.error_code == "proxy_selection_mismatch"
    assert "confirm_proxy" not in ssh.actions
    await engine.dispose()


def test_consistency_monitor_detects_selection_mismatch() -> None:
    expected = {"kind": "node", "resource_id": 1, "fingerprint": "a" * 64}
    issues = TaskRunner._consistency_issues(
        ProxyMode.proxy,
        expected,
        {
            "singbox_active": "active",
            "config_consistent": True,
            "connectivity_ok": True,
            "active_config": {
                "config_sha256": "b" * 64,
                "selection": {**expected, "resource_id": 2},
            },
        },
    )

    assert "数据库选中资源与远端已加载资源不一致" in issues


@pytest.mark.asyncio
async def test_consistency_monitor_queues_codex_diagnosis() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    key = generate_key()
    secret_box = SecretBox(key)
    async with factory() as session:
        host = await Repository(session, secret_box).add_host(
            name="monitored",
            host="203.0.113.30",
            port=22,
            username="root",
            auth_method=AuthMethod.password,
            secret="test-only",  # noqa: S106
            known_host="known-host",
        )
        await session.commit()
        host_id = host.id
    runner = TaskRunner(factory, settings(key), secret_box, ssh_client=FakeSSH())

    await runner._record_monitor_failure(
        host_id,
        ["数据库期望本地出口，但 sing-box 仍在运行"],
        ProxyMode.local,
        None,
        {"singbox_active": "active", "connectivity_ok": True},
    )

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.kind == TaskKind.consistency_check))
        assert task is not None and task.status == TaskStatus.failed
        assert task.error_code == "proxy_state_mismatch"
        diagnostic = await session.scalar(
            select(CodexTask).where(CodexTask.source_task_id == task.id)
        )
        assert diagnostic is not None and diagnostic.operation == "diagnose"
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_retest_marks_matching_failure_resolved() -> None:
    engine, factory, ssh, failed_id, host_id, runner = await create_apply_task(False)
    await runner._run_one(failed_id)

    async with factory() as session:
        failed = await session.get(Task, failed_id)
        assert failed is not None
        retest = await Repository(session, runner.secret_box).create_task(
            kind=failed.kind,
            actor_user_id=failed.actor_user_id,
            host_id=host_id,
            payload=failed.payload,
        )
        await session.commit()
        retest_id = retest.id

    ssh.connectivity_ok = True
    await runner._run_one(retest_id)

    async with factory() as session:
        failed = await session.get(Task, failed_id)
        retest = await session.get(Task, retest_id)
        assert failed is not None and retest is not None
        assert retest.status == TaskStatus.succeeded
        assert failed.result["resolved_by_task_id"] == retest.id
        assert f"任务 #{retest.id} 验证解决" in failed.message
    await engine.dispose()


@pytest.mark.asyncio
async def test_vps_subscription_commits_entries_before_progress_updates() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    key = generate_key()
    secret_box = SecretBox(key)
    spec = parse_node_link(VLESS_REALITY)
    async with factory() as session:
        repo = Repository(session, secret_box)
        source = await repo.create_subscription(
            "subscription", "https://example.com/sub", VLESS_REALITY, [spec]
        )
        host = await repo.add_host(
            name="target",
            host="203.0.113.20",
            port=22,
            username="root",
            auth_method=AuthMethod.password,
            secret="test-only",  # noqa: S106
            known_host="known-host",
        )
        assigned = await repo.assign_subscription(host, source)
        task = await repo.create_task(
            kind=TaskKind.vps_subscription_test,
            actor_user_id=1,
            host_id=host.id,
            payload={"vps_subscription_id": assigned.id},
        )
        await session.commit()
        task_id = task.id
    runner = TransactionCheckingRunner(
        factory, settings(key), secret_box, ssh_client=SubscriptionSSH()
    )

    await runner._run_one(task_id)

    async with factory() as session:
        task = await session.get(Task, task_id)
        entries = await Repository(session, secret_box).list_vps_subscription_entries(assigned.id)
        assert task is not None and task.status == TaskStatus.succeeded
        assert len(entries) == 1
        assert entries[0].last_latency_ms == 4
    await engine.dispose()
