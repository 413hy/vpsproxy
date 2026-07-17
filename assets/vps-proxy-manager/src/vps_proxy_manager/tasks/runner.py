from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import (
    ProxyMode,
    ResourceKind,
    Task,
    TaskKind,
    TaskStatus,
    VpsNode,
    VpsSubscription,
    VpsSubscriptionEntry,
)
from vps_proxy_manager.proxy.parser import ProxyNodeSpec, parse_node_blob, parse_subscription_text
from vps_proxy_manager.proxy.singbox import build_speedtest_config, build_tun_config
from vps_proxy_manager.proxy.ssrf import fetch_subscription
from vps_proxy_manager.proxy.tester import LocalProxyTester
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.ssh.client import SSHClient, SSHError, credentials_from_host
from vps_proxy_manager.utils.redact import redact_text

log = structlog.get_logger()


NETWORK_MUTATING = {
    TaskKind.sync_node,
    TaskKind.sync_subscription,
    TaskKind.apply_proxy,
    TaskKind.stop_proxy,
    TaskKind.restore_proxy,
    TaskKind.rollback,
    TaskKind.remove_vps_node,
    TaskKind.remove_vps_subscription,
    TaskKind.delete_source_node,
    TaskKind.delete_source_subscription,
    TaskKind.uninstall,
    TaskKind.delete_host,
}

RESTART_SAFE = {
    TaskKind.detect,
    TaskKind.status,
    TaskKind.test_ssh,
    TaskKind.local_node_test,
    TaskKind.local_subscription_test,
    TaskKind.vps_node_test,
    TaskKind.vps_subscription_test,
}

NON_DIAGNOSABLE_ERRORS = {"host_busy"}


class TaskCanceled(RuntimeError):
    pass


class TaskRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        secret_box: SecretBox,
        ssh_client: SSHClient | None = None,
        local_tester: LocalProxyTester | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.secret_box = secret_box
        self.ssh = ssh_client or SSHClient()
        self.local_tester = local_tester or LocalProxyTester()
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_ids: set[int] = set()
        self._host_locks: dict[int, asyncio.Lock] = {}
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            return
        await self._recover_tasks()
        self._worker = asyncio.create_task(self._work(), name="vpspm-task-runner")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def enqueue(self, task_id: int) -> None:
        if task_id not in self._queued_ids:
            self._queued_ids.add(task_id)
            await self.queue.put(task_id)

    async def request_cancel(self, task_id: int) -> bool:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status not in {TaskStatus.queued, TaskStatus.running}:
                return False
            task.status = TaskStatus.cancel_requested
            task.message = "正在请求取消"
            await session.commit()
            return True

    async def _recover_tasks(self) -> None:
        async with self.session_factory() as session:
            active = (
                await session.scalars(
                    select(Task).where(
                        Task.status.in_(
                            [TaskStatus.queued, TaskStatus.running, TaskStatus.cancel_requested]
                        )
                    )
                )
            ).all()
            for task in active:
                if task.status == TaskStatus.queued and task.kind in RESTART_SAFE:
                    await self.enqueue(task.id)
                    continue
                task.status = TaskStatus.failed
                task.error_code = "controller_restarted"
                task.message = "控制端重启，任务未自动重放；请手动确认后重试"
                task.finished_at = datetime.now(UTC)
                await self._queue_codex_diagnostic(session, task)
            await session.commit()

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            self._queued_ids.discard(task_id)
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
            if task.status == TaskStatus.cancel_requested:
                await self._finish(session, task, TaskStatus.canceled, None, "任务已取消")
                return
            if task.status != TaskStatus.queued:
                return
            task.status = TaskStatus.running
            task.started_at = datetime.now(UTC)
            task.progress = 1
            task.message = "任务已开始"
            await session.commit()

        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            lock = (
                self._host_locks.setdefault(task.host_id, asyncio.Lock())
                if task.host_id and task.kind in NETWORK_MUTATING
                else None
            )
            if lock and lock.locked():
                await self._finish(
                    session,
                    task,
                    TaskStatus.failed,
                    "host_busy",
                    "该 VPS 正在执行网络修改任务",
                )
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
            TaskKind.status: self._status,
            TaskKind.local_node_test: self._local_node_test,
            TaskKind.local_subscription_test: self._local_subscription_test,
            TaskKind.sync_node: self._sync_node,
            TaskKind.sync_subscription: self._sync_subscription,
            TaskKind.vps_node_test: self._vps_node_test,
            TaskKind.vps_subscription_test: self._vps_subscription_test,
            TaskKind.apply_proxy: self._apply_proxy,
            TaskKind.stop_proxy: self._simple_remote("stop_proxy", "已切回 VPS 本地出口"),
            TaskKind.restore_proxy: self._restore_proxy,
            TaskKind.rollback: self._rollback_proxy,
            TaskKind.remove_vps_node: self._remove_vps_node,
            TaskKind.remove_vps_subscription: self._remove_vps_subscription,
            TaskKind.delete_source_node: self._delete_source_node,
            TaskKind.delete_source_subscription: self._delete_source_subscription,
            TaskKind.uninstall: self._simple_remote(
                "uninstall", "代理组件已卸载，VPS 使用本地出口"
            ),
            TaskKind.delete_host: self._delete_host,
        }
        handler = handlers.get(task.kind)
        if handler is None:
            await self._finish(
                session, task, TaskStatus.failed, "unsupported_task", "不支持的任务类型"
            )
            return
        task_id = task.id
        task_kind = task.kind.value
        try:
            await handler(session, task)
        except TaskCanceled:
            await session.rollback()
            recovered = await session.get(Task, task_id)
            if recovered:
                await self._finish(session, recovered, TaskStatus.canceled, None, "任务已取消")
        except SSHError as exc:
            await session.rollback()
            recovered = await session.get(Task, task_id)
            if recovered:
                await self._finish(
                    session,
                    recovered,
                    TaskStatus.failed,
                    exc.code,
                    str(exc),
                    {
                        "exception_type": type(exc).__name__,
                        "technical_error": redact_text(str(exc))[:1500],
                    },
                )
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            log.exception("task_failed", task_id=task_id, kind=task_kind, error=str(exc))
            recovered = await session.get(Task, task_id)
            if recovered:
                await self._finish(
                    session,
                    recovered,
                    TaskStatus.failed,
                    "internal_error",
                    "任务失败，正在交给 Codex 自动诊断",
                    {
                        "exception_type": type(exc).__name__,
                        "technical_error": redact_text(str(exc))[:1500],
                    },
                )

    async def _detect(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        result = await self.ssh.run_agent(creds, "status", {}, timeout=45)
        host.last_status = result.get("status", {})
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action=task.kind.value,
            result="ok",
            host_id=host.id,
        )
        await self._finish(
            session, task, TaskStatus.succeeded, None, "SSH 与远端 Agent 检测完成", result
        )

    async def _status(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        result = await self.ssh.run_agent(creds, "status", {}, timeout=45)
        status = result.get("status", {})
        state = await repo.get_proxy_state(host.id)
        if isinstance(status, dict):
            service = status.get("singbox_active")
            if status.get("managed_library_exists") is False:
                state.mode = ProxyMode.uninstalled
            else:
                state.mode = ProxyMode.proxy if service == "active" else ProxyMode.local
            status["exit_mode"] = state.mode.value
            host.last_status = status
            host.remote_agent_version = status.get("agent_version") or host.remote_agent_version
        await repo.audit(
            actor_user_id=task.actor_user_id, action="status", result="ok", host_id=host.id
        )
        await self._finish(session, task, TaskStatus.succeeded, None, "状态已刷新", result)

    async def _local_node_test(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        node_ids = [int(item) for item in task.payload.get("node_ids", [])]
        if node_ids:
            nodes = [await repo.get_node(item) for item in node_ids]
        else:
            nodes = list(await repo.list_nodes(limit=1000))
        if not nodes:
            raise ValueError("no nodes available")
        specs = [(node, parse_node_blob(repo.decrypt_node_link(node))) for node in nodes]
        results = await self._run_local_tests(session, task, specs)
        for node, result in results:
            await repo.update_node_test(node, result)
        await repo.audit(actor_user_id=task.actor_user_id, action="local_node_test", result="ok")
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"控制端单节点测速完成：{sum(bool(result.get('proxy_ok')) for _, result in results)}/{len(results)} 可用",
            {"summary": self._test_summary(results)},
        )

    async def _local_subscription_test(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        sub = await repo.get_subscription(int(task.payload["subscription_id"]))
        if task.payload.get("refresh"):
            content = await fetch_subscription(
                repo.decrypt_subscription_url(sub),
                timeout_seconds=self.settings.subscription_timeout_seconds,
                max_bytes=self.settings.subscription_max_bytes,
                max_redirects=self.settings.subscription_max_redirects,
                allow_private=self.settings.allow_private_subscription_urls,
            )
            specs = parse_subscription_text(content)
            await repo.update_subscription_content(sub, content, specs)
            # Progress updates use short independent sessions. Release the subscription
            # refresh write transaction before those updates start.
            await session.commit()
        entries = list(await repo.list_subscription_entries(sub.id, limit=10000))
        if not entries:
            raise ValueError("subscription has no testable nodes")
        pairs = [(entry, parse_node_blob(repo.decrypt_node_link(entry))) for entry in entries]
        results = await self._run_local_tests(session, task, pairs)
        for entry, result in results:
            await repo.update_node_test(entry, result)
        summary = self._test_summary(results)
        sub.last_test = summary
        await repo.audit(
            actor_user_id=task.actor_user_id, action="local_subscription_test", result="ok"
        )
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"控制端订阅测速完成：{summary['online']}/{summary['total']} 可用",
            {"summary": summary},
        )

    async def _run_local_tests(
        self,
        session: AsyncSession,
        task: Task,
        pairs: Sequence[tuple[Any, ProxyNodeSpec]],
    ) -> list[tuple[Any, dict[str, Any]]]:
        sem = asyncio.Semaphore(self.settings.speedtest_concurrency)
        completed = 0
        progress_lock = asyncio.Lock()

        async def test_one(item: Any, spec: ProxyNodeSpec) -> tuple[Any, dict[str, Any]]:
            nonlocal completed
            async with sem:
                await self._check_canceled(task.id)
                try:
                    result = await self.local_tester.test(spec)
                except Exception as exc:  # noqa: BLE001
                    result = self._failed_test(str(exc))
                async with progress_lock:
                    completed += 1
                    await self._progress(
                        task.id,
                        max(5, int(completed / len(pairs) * 95)),
                        f"正在测速 {completed}/{len(pairs)}",
                    )
                return item, result

        return await self._gather_tests([test_one(item, spec) for item, spec in pairs])

    async def _sync_node(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        node = await repo.get_node(int(task.payload["node_id"]))
        assignment = await repo.assign_node(host, node)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        await self.ssh.run_agent(
            creds,
            "store_node",
            {
                "library_id": assignment.id,
                "name": assignment.name,
                "link": repo.decrypt_node_link(assignment),
            },
            timeout=60,
        )
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="sync_node",
            result="ok",
            host_id=host.id,
            detail={"node_id": node.id},
        )
        await self._finish(session, task, TaskStatus.succeeded, None, "节点已导入目标 VPS")

    async def _sync_subscription(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        source = await repo.get_subscription(int(task.payload["subscription_id"]))
        assignment = await repo.assign_subscription(host, source)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        await self.ssh.run_agent(
            creds,
            "store_subscription",
            {
                "library_id": assignment.id,
                "name": assignment.name,
                "url": repo.decrypt_subscription_url(assignment),
            },
            timeout=60,
        )
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="sync_subscription",
            result="ok",
            host_id=host.id,
            detail={"subscription_id": source.id},
        )
        await self._finish(session, task, TaskStatus.succeeded, None, "完整订阅已导入目标 VPS")

    async def _vps_node_test(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        item_ids = [int(item) for item in task.payload.get("vps_node_ids", [])]
        items = (
            [await repo.get_vps_node(item) for item in item_ids]
            if item_ids
            else list(await repo.list_vps_nodes(host.id))
        )
        for item in items:
            if item.host_id != host.id:
                raise ValueError("VPS node does not belong to host")
        results = await self._run_remote_tests(session, task, host, items)
        for item, result in results:
            await repo.update_node_test(item, result)
        summary = self._test_summary(results)
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"目标 VPS 单节点测速完成：{summary['online']}/{summary['total']} 可用",
            {"summary": summary},
        )

    async def _vps_subscription_test(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        sub = await repo.get_vps_subscription(int(task.payload["vps_subscription_id"]))
        if sub.host_id != host.id:
            raise ValueError("VPS subscription does not belong to host")
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        fetched = await self.ssh.run_agent(
            creds,
            "fetch_subscription",
            {
                "library_id": sub.id,
                "timeout": self.settings.subscription_timeout_seconds,
                "max_bytes": self.settings.subscription_max_bytes,
                "max_redirects": self.settings.subscription_max_redirects,
            },
            timeout=self.settings.subscription_timeout_seconds + 30,
        )
        body = base64.b64decode(str(fetched["content_b64"]), validate=True)
        content = body.decode("utf-8", errors="replace")
        specs = parse_subscription_text(content)
        sub.encrypted_content = self.secret_box.encrypt(content)
        entries = await repo.replace_vps_subscription_entries(sub, specs)
        # Do not hold SQLite's writer lock while concurrent tests publish progress.
        await session.commit()
        results = await self._run_remote_tests(session, task, host, entries)
        for item, result in results:
            await repo.update_node_test(item, result)
        summary = self._test_summary(results)
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"目标 VPS 订阅测速完成：{summary['online']}/{summary['total']} 可用",
            {"summary": summary},
        )

    async def _run_remote_tests(
        self,
        session: AsyncSession,
        task: Task,
        host: Any,
        items: Sequence[VpsNode | VpsSubscriptionEntry],
    ) -> list[tuple[Any, dict[str, Any]]]:
        if not items:
            raise ValueError("no target nodes available")
        repo = Repository(session, self.secret_box)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        sem = asyncio.Semaphore(self.settings.speedtest_concurrency)
        completed = 0
        progress_lock = asyncio.Lock()

        async def test_one(index: int, item: Any) -> tuple[Any, dict[str, Any]]:
            nonlocal completed
            async with sem:
                await self._check_canceled(task.id)
                try:
                    spec = parse_node_blob(repo.decrypt_node_link(item))
                    result = await self.ssh.run_agent(
                        creds,
                        "speedtest",
                        {
                            "config": build_speedtest_config(spec, 18080 + index),
                            "listen_port": 18080 + index,
                            "attempts": 3,
                        },
                        timeout=90,
                    )
                    test = result.get("result", {})
                except Exception as exc:  # noqa: BLE001
                    test = self._failed_test(str(exc))
                async with progress_lock:
                    completed += 1
                    await self._progress(
                        task.id,
                        max(5, int(completed / len(items) * 95)),
                        f"目标 VPS 正在测速 {completed}/{len(items)}",
                    )
                return item, test

        return await self._gather_tests([test_one(index, item) for index, item in enumerate(items)])

    async def _apply_proxy(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        state = await repo.get_proxy_state(host.id)
        item: VpsNode | VpsSubscriptionEntry
        if task.payload.get("vps_node_id"):
            item = await repo.get_vps_node(int(task.payload["vps_node_id"]))
            if item.host_id != host.id:
                raise ValueError("VPS node does not belong to host")
            kind = ResourceKind.node
            subscription_id = None
            fingerprint = None
        else:
            item = await repo.get_vps_subscription_entry(
                int(task.payload["vps_subscription_entry_id"])
            )
            sub = await repo.get_vps_subscription(item.vps_subscription_id)
            if sub.host_id != host.id:
                raise ValueError("VPS subscription entry does not belong to host")
            kind = ResourceKind.subscription
            subscription_id = sub.id
            fingerprint = item.fingerprint
        spec = parse_node_blob(repo.decrypt_node_link(item))
        previous_state = {
            "mode": state.mode.value,
            "current_kind": state.current_kind.value if state.current_kind else None,
            "current_vps_node_id": state.current_vps_node_id,
            "current_vps_subscription_id": state.current_vps_subscription_id,
            "current_entry_fingerprint": state.current_entry_fingerprint,
            "current_display_name": state.current_display_name,
        }
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        previous_system = host.system_info or {}
        detected = await self.ssh.run_agent(creds, "detect", {}, timeout=30)
        current_system = detected.get("system", {})
        if isinstance(current_system, dict):
            host.system_info = current_system
        ssh_client_ip = current_system.get("ssh_client_ip") or previous_system.get("ssh_client_ip")
        config = build_tun_config(
            spec,
            management_source_ip=ssh_client_ip,
            ssh_port=host.port,
            auto_redirect=True,
        )
        await self._progress(task.id, 20, "已生成配置，正在设置自动回滚保护")
        result = await self.ssh.run_agent(
            creds,
            "apply_proxy",
            {"config": config, "rollback_seconds": self.settings.remote_rollback_seconds},
            timeout=360,
        )
        await self._progress(task.id, 75, "代理已启动，正在确认 SSH 和出口状态")
        status_result = await self.ssh.run_agent(creds, "status", {}, timeout=45)
        status = status_result.get("status", {})
        if status.get("singbox_active") != "active" or not status.get("connectivity_ok"):
            raise SSHError(
                "proxy_verification_failed",
                "代理启动后 SSH 或公网访问验证失败，自动回滚定时器仍有效",
            )
        await self.ssh.run_agent(creds, "confirm_proxy", {}, timeout=30)
        state.mode = ProxyMode.proxy
        state.current_kind = kind
        state.current_vps_node_id = item.id if kind == ResourceKind.node else None
        state.current_vps_subscription_id = subscription_id
        state.current_entry_fingerprint = fingerprint
        state.current_display_name = item.name
        state.last_switch_at = datetime.now(UTC)
        host.config_version += 1
        host.last_status = {**status, "exit_mode": "proxy"}
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="apply_proxy",
            result="ok",
            host_id=host.id,
            detail={"kind": kind.value, "name": item.name},
        )
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            "代理已应用，SSH 连通性确认成功，自动回滚保护已解除",
            {
                "backup": result.get("backup"),
                "status": status,
                "previous_state": previous_state,
            },
        )

    async def _restore_proxy(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        await self.ssh.run_agent(creds, "restore_proxy", {}, timeout=180)
        status_result = await self.ssh.run_agent(creds, "status", {}, timeout=45)
        status = status_result.get("status", {})
        if status.get("singbox_active") != "active" or not status.get("connectivity_ok"):
            raise SSHError(
                "proxy_verification_failed",
                "恢复代理后 SSH 或公网访问验证失败，自动回滚定时器仍有效",
            )
        await self.ssh.run_agent(creds, "confirm_proxy", {}, timeout=30)
        state = await repo.get_proxy_state(host.id)
        state.mode = ProxyMode.proxy
        host.last_status = {**status, "exit_mode": "proxy"}
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="restore_proxy",
            result="ok",
            host_id=host.id,
        )
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            "上次代理已恢复，SSH 与公网访问验证成功",
            status_result,
        )

    async def _rollback_proxy(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        result = await self.ssh.run_agent(creds, "rollback", {}, timeout=180)
        status_result = await self.ssh.run_agent(creds, "status", {}, timeout=45)
        status = status_result.get("status", {})
        previous_task = await session.scalar(
            select(Task)
            .where(
                Task.host_id == host.id,
                Task.kind == TaskKind.apply_proxy,
                Task.status == TaskStatus.succeeded,
            )
            .order_by(Task.finished_at.desc())
            .limit(1)
        )
        state = await repo.get_proxy_state(host.id)
        previous = previous_task.result.get("previous_state", {}) if previous_task else {}
        if previous:
            state.mode = ProxyMode(previous.get("mode", ProxyMode.local.value))
            kind_value = previous.get("current_kind")
            state.current_kind = ResourceKind(kind_value) if kind_value else None
            state.current_vps_node_id = previous.get("current_vps_node_id")
            state.current_vps_subscription_id = previous.get("current_vps_subscription_id")
            state.current_entry_fingerprint = previous.get("current_entry_fingerprint")
            state.current_display_name = previous.get("current_display_name")
        else:
            state.mode = (
                ProxyMode.proxy if status.get("singbox_active") == "active" else ProxyMode.local
            )
        host.last_status = {**status, "exit_mode": state.mode.value}
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="rollback",
            result="ok",
            host_id=host.id,
        )
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"已恢复上一配置，当前出口模式：{state.mode.value}",
            {**result, "status": status},
        )

    def _simple_remote(
        self, action: str, message: str
    ) -> Callable[[AsyncSession, Task], Awaitable[None]]:
        async def handler(session: AsyncSession, task: Task) -> None:
            repo = Repository(session, self.secret_box)
            host = await repo.get_host(task.host_id or 0)
            creds = credentials_from_host(host, repo.decrypt_host_secret(host))
            result = await self.ssh.run_agent(creds, action, {}, timeout=180)
            state = await repo.get_proxy_state(host.id)
            if action == "restore_proxy":
                state.mode = ProxyMode.proxy
                host.last_status = {"singbox_active": "active", "exit_mode": "proxy"}
            elif action in {"stop_proxy", "rollback"}:
                state.mode = ProxyMode.local
                host.last_status = {"singbox_active": "inactive", "exit_mode": "local"}
            elif action == "uninstall":
                state.mode = ProxyMode.uninstalled
                state.current_kind = None
                state.current_vps_node_id = None
                state.current_vps_subscription_id = None
                state.current_entry_fingerprint = None
                state.current_display_name = None
                host.last_status = {"singbox_active": "inactive", "exit_mode": "local"}
            await repo.audit(
                actor_user_id=task.actor_user_id,
                action=action,
                result="ok",
                host_id=host.id,
            )
            await self._finish(session, task, TaskStatus.succeeded, None, message, result)

        return handler

    async def _remove_vps_node(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        item = await repo.get_vps_node(int(task.payload["vps_node_id"]))
        if item.host_id != host.id:
            raise ValueError("VPS node does not belong to host")
        state = await repo.get_proxy_state(host.id)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        if state.current_kind == ResourceKind.node and state.current_vps_node_id == item.id:
            await self.ssh.run_agent(creds, "stop_proxy", {}, timeout=60)
            state.mode = ProxyMode.local
            state.current_kind = None
            state.current_vps_node_id = None
            state.current_display_name = None
        await self.ssh.run_agent(creds, "remove_node", {"library_id": item.id}, timeout=60)
        await session.execute(delete(VpsNode).where(VpsNode.id == item.id))
        await self._finish(session, task, TaskStatus.succeeded, None, "节点已从目标 VPS 删除")

    async def _remove_vps_subscription(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        sub = await repo.get_vps_subscription(int(task.payload["vps_subscription_id"]))
        if sub.host_id != host.id:
            raise ValueError("VPS subscription does not belong to host")
        state = await repo.get_proxy_state(host.id)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        if (
            state.current_kind == ResourceKind.subscription
            and state.current_vps_subscription_id == sub.id
        ):
            await self.ssh.run_agent(creds, "stop_proxy", {}, timeout=60)
            state.mode = ProxyMode.local
            state.current_kind = None
            state.current_vps_subscription_id = None
            state.current_entry_fingerprint = None
            state.current_display_name = None
        await self.ssh.run_agent(creds, "remove_subscription", {"library_id": sub.id}, timeout=60)
        await session.execute(delete(VpsSubscription).where(VpsSubscription.id == sub.id))
        await self._finish(session, task, TaskStatus.succeeded, None, "订阅已从目标 VPS 删除")

    async def _delete_source_node(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        node = await repo.get_node(int(task.payload["node_id"]))
        usage = list(await repo.node_usage(node.id))
        for index, item in enumerate(usage, start=1):
            await self._check_canceled(task.id)
            host = await repo.get_host(item.host_id)
            lock = self._host_locks.setdefault(host.id, asyncio.Lock())
            async with lock:
                creds = credentials_from_host(host, repo.decrypt_host_secret(host))
                state = await repo.get_proxy_state(host.id)
                if state.current_kind == ResourceKind.node and state.current_vps_node_id == item.id:
                    await self.ssh.run_agent(creds, "stop_proxy", {}, timeout=60)
                    state.mode = ProxyMode.local
                    state.current_kind = None
                    state.current_vps_node_id = None
                    state.current_display_name = None
                await self.ssh.run_agent(creds, "remove_node", {"library_id": item.id}, timeout=60)
                await session.execute(delete(VpsNode).where(VpsNode.id == item.id))
                await session.commit()
            await self._progress(
                task.id,
                int(index / max(len(usage), 1) * 90),
                f"正在从 VPS 删除节点 {index}/{len(usage)}",
            )
        await repo.delete_node(node.id)
        await self._finish(session, task, TaskStatus.succeeded, None, "单节点及其 VPS 副本已删除")

    async def _delete_source_subscription(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        source = await repo.get_subscription(int(task.payload["subscription_id"]))
        usage = list(await repo.subscription_usage(source.id))
        for index, sub in enumerate(usage, start=1):
            await self._check_canceled(task.id)
            host = await repo.get_host(sub.host_id)
            lock = self._host_locks.setdefault(host.id, asyncio.Lock())
            async with lock:
                creds = credentials_from_host(host, repo.decrypt_host_secret(host))
                state = await repo.get_proxy_state(host.id)
                if (
                    state.current_kind == ResourceKind.subscription
                    and state.current_vps_subscription_id == sub.id
                ):
                    await self.ssh.run_agent(creds, "stop_proxy", {}, timeout=60)
                    state.mode = ProxyMode.local
                    state.current_kind = None
                    state.current_vps_subscription_id = None
                    state.current_entry_fingerprint = None
                    state.current_display_name = None
                await self.ssh.run_agent(
                    creds, "remove_subscription", {"library_id": sub.id}, timeout=60
                )
                await session.execute(delete(VpsSubscription).where(VpsSubscription.id == sub.id))
                await session.commit()
            await self._progress(
                task.id,
                int(index / max(len(usage), 1) * 90),
                f"正在从 VPS 删除订阅 {index}/{len(usage)}",
            )
        await repo.delete_subscription(source.id)
        await self._finish(session, task, TaskStatus.succeeded, None, "订阅及其 VPS 副本已删除")

    async def _delete_host(self, session: AsyncSession, task: Task) -> None:
        repo = Repository(session, self.secret_box)
        host = await repo.get_host(task.host_id or 0)
        creds = credentials_from_host(host, repo.decrypt_host_secret(host))
        if bool(task.payload.get("uninstall")):
            await self.ssh.run_agent(creds, "uninstall", {}, timeout=180)
        host_name = host.name
        task.host_id = None
        await session.flush()
        await repo.delete_host_record(host.id)
        await self._finish(
            session,
            task,
            TaskStatus.succeeded,
            None,
            f"VPS {host_name} 已从管理系统删除",
        )

    async def _check_canceled(self, task_id: int) -> None:
        async with self.session_factory() as session:
            status = await session.scalar(select(Task.status).where(Task.id == task_id))
        if status == TaskStatus.cancel_requested:
            raise TaskCanceled

    @staticmethod
    async def _gather_tests(
        awaitables: Sequence[Awaitable[tuple[Any, dict[str, Any]]]],
    ) -> list[tuple[Any, dict[str, Any]]]:
        workers: list[asyncio.Task[tuple[Any, dict[str, Any]]]] = [
            asyncio.create_task(item) for item in awaitables
        ]
        try:
            return list(await asyncio.gather(*workers))
        except BaseException:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

    async def _progress(self, task_id: int, value: int, message: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(Task, task_id)
            if task and task.status == TaskStatus.running:
                task.progress = min(max(value, 0), 99)
                task.message = message
                await session.commit()

    @staticmethod
    def _failed_test(error: str) -> dict[str, Any]:
        return {
            "dns_ok": False,
            "dns_latency_ms": None,
            "tcp_ok": False,
            "tcp_latency_ms": None,
            "proxy_ok": False,
            "proxy_handshake_ms": None,
            "access_latency_ms": None,
            "latency_ms": None,
            "error": error[:500],
        }

    @staticmethod
    def _test_summary(results: Sequence[tuple[Any, dict[str, Any]]]) -> dict[str, Any]:
        latencies = [
            int(result["access_latency_ms"])
            for _, result in results
            if result.get("proxy_ok") and result.get("access_latency_ms") is not None
        ]
        return {
            "total": len(results),
            "online": sum(bool(result.get("proxy_ok")) for _, result in results),
            "offline": sum(not bool(result.get("proxy_ok")) for _, result in results),
            "average_access_latency_ms": int(sum(latencies) / len(latencies))
            if latencies
            else None,
            "tested_at": datetime.now(UTC).isoformat(),
        }

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
        if status == TaskStatus.failed:
            await self._queue_codex_diagnostic(session, task)
        await session.commit()

    async def _queue_codex_diagnostic(self, session: AsyncSession, task: Task) -> None:
        if (
            not self.settings.codex_enabled
            or task.error_code in NON_DIAGNOSABLE_ERRORS
            or task.id is None
        ):
            return
        repo = Repository(session, self.secret_box)
        diagnostic = await repo.create_codex_diagnostic(task.id)
        task.result = {
            **(task.result or {}),
            "codex_diagnostic_task_id": diagnostic.id,
        }
        if f"Codex 自动诊断 #{diagnostic.id}" not in task.message:
            task.message = f"{task.message}；Codex 自动诊断 #{diagnostic.id} 已排队"
        await repo.audit(
            actor_user_id=task.actor_user_id,
            action="codex_diagnosis_queued",
            result="queued",
            host_id=task.host_id,
            detail={"source_task_id": task.id, "codex_task_id": diagnostic.id},
        )
