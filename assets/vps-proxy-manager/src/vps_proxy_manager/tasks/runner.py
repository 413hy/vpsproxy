from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import ProxyNode, Task, TaskKind, TaskStatus
from vps_proxy_manager.proxy.parser import parse_node_link
from vps_proxy_manager.proxy.singbox import build_speedtest_config, build_tun_config
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.ssh.client import SSHClient, SSHError, credentials_from_host

log = structlog.get_logger()


NETWORK_MUTATING = {
    TaskKind.apply_proxy,
    TaskKind.stop_proxy,
    TaskKind.restore_proxy,
    TaskKind.rollback,
    TaskKind.uninstall,
}


class TaskRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        secret_box: SecretBox,
        ssh_client: SSHClient | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.secret_box = secret_box
        self.ssh = ssh_client or SSHClient()
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._host_locks: dict[int, asyncio.Lock] = {}
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work(), name="vpspm-task-runner")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def enqueue(self, task_id: int) -> None:
        await self.queue.put(task_id)

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                await self._run_one(task_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("task_runner_unhandled", task_id=task_id, error=str(exc))
            finally:
                self.queue.task_done()

    async def _run_one(self, task_id: int) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = TaskStatus.running
            task.started_at = datetime.now(UTC)
            await session.commit()

        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            lock = self._host_locks.setdefault(task.host_id, asyncio.Lock()) if task.host_id and task.kind in NETWORK_MUTATING else None
            if lock and lock.locked():
                await self._finish(session, task, TaskStatus.failed, "host_busy", "该 VPS 正在执行网络修改任务")
                return
            if lock:
                async with lock:
                    await self._dispatch(session, task)
            else:
                await self._dispatch(session, task)

    async def _dispatch(self, session: AsyncSession, task: Task) -> None:
        handlers: dict[TaskKind, Callable[[AsyncSession, Task], Awaitable[None]]] = {
            TaskKind.detect: self._detect,
            TaskKind.test_ssh: self._detect,
            TaskKind.speedtest: self._speedtest,
            TaskKind.apply_proxy: self._apply_proxy,
            TaskKind.stop_proxy: self._simple_remote("stop_proxy", "代理已停止"),
            TaskKind.restore_proxy: self._simple_remote("restore_proxy", "代理已恢复"),
            TaskKind.rollback: self._simple_remote("rollback", "已回滚上一配置"),
            TaskKind.uninstall: self._simple_remote("uninstall", "已卸载并保留备份"),
        }
        try:
            await handlers[task.kind](session, task)
        except SSHError as exc:
            await self._finish(session, task, TaskStatus.failed, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("task_failed", task_id=task.id, kind=task.kind.value, error=str(exc))
            await self._finish(session, task, TaskStatus.failed, "internal_error", "任务失败，请查看脱敏日志")

    async def _detect(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        result = await self.ssh.run_payload(creds, "detect", sudo=False, timeout=45)
        host.system_info = result.get("system", {})
        host.last_status = {"ssh": "ok"}
        await repo.audit(actor_user_id=task.actor_user_id, action=task.kind.value, result="ok", host_id=host.id)
        await self._finish(session, task, TaskStatus.succeeded, None, "检测完成", result)

    def _simple_remote(self, action: str, message: str) -> Callable[[AsyncSession, Task], Awaitable[None]]:
        async def handler(session: AsyncSession, task: Task) -> None:
            repo = Repository(session, self.secret_box)
            host = await repo.get_host(task.host_id or 0)
            creds = credentials_from_host(host, repo.decrypt_host_secret(host))
            result = await self.ssh.run_payload(creds, action, {}, timeout=180)
            await repo.audit(actor_user_id=task.actor_user_id, action=action, result="ok", host_id=host.id)
            await self._finish(session, task, TaskStatus.succeeded, None, message, result)

        return handler

    async def _speedtest(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        node_ids = task.payload.get("node_ids") or []
        if not node_ids:
            node_ids = list((await session.scalars(select(ProxyNode.id))).all())
        if not node_ids:
            raise ValueError("no nodes available")
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(self.settings.speedtest_concurrency)

        async def test_one(node_id: int, index: int) -> None:
            async with sem:
                node = await repo.get_node(node_id)
                spec = parse_node_link(repo.decrypt_node_link(node))
                config = build_speedtest_config(spec, 18080 + index)
                result = await self.ssh.run_payload(
                    creds,
                    "speedtest",
                    {"config": config, "listen_port": 18080 + index},
                    timeout=90,
                )
                test = result.get("result", {})
                await repo.update_node_test(node, test)
                results.append({"node_id": node_id, **test})

        await asyncio.gather(*(test_one(int(node_id), idx) for idx, node_id in enumerate(node_ids)))
        await repo.audit(actor_user_id=task.actor_user_id, action="speedtest", result="ok", host_id=host.id)
        await self._finish(session, task, TaskStatus.succeeded, None, "测速完成", {"results": results})

    async def _apply_proxy(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        node = await repo.get_node(int(task.payload["node_id"]))
        spec = parse_node_link(repo.decrypt_node_link(node))
        ssh_client_ip = (host.system_info or {}).get("ssh_client_ip")
        config = build_tun_config(
            spec,
            management_source_ip=ssh_client_ip,
            ssh_port=host.port,
            auto_redirect=True,
        )
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        result = await self.ssh.run_payload(
            creds,
            "apply_proxy",
            {"config": config, "rollback_seconds": self.settings.remote_rollback_seconds},
            timeout=360,
        )
        status = await self.ssh.run_payload(creds, "status", {}, timeout=45)
        await self.ssh.run_payload(creds, "confirm_proxy", {}, timeout=30)
        await repo.set_host_current_node(host, node)
        host.last_status = status.get("status", {})
        await repo.audit(actor_user_id=task.actor_user_id, action="apply_proxy", result="ok", host_id=host.id)
        await self._finish(session, task, TaskStatus.succeeded, None, "代理已应用并确认 SSH 未断开", result)

    async def _finish(
        self,
        session: AsyncSession,
        task: Task,
        status: TaskStatus,
        code: str | None,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        task.status = status
        task.error_code = code
        task.message = message
        task.result = result or {}
        task.progress = 100
        task.finished_at = datetime.now(UTC)
        await session.commit()
