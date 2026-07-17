from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import CodexTaskStatus, HostLifecycle
from vps_proxy_manager.services.repository import Repository
from vps_proxy_manager.ssh.client import SSHClient, SSHError, credentials_from_candidate
from vps_proxy_manager.utils.redact import redact_obj, redact_text

log = structlog.get_logger()


class CodexWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        secret_box: SecretBox,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.secret_box = secret_box
        self._stopping = False

    async def run_forever(self) -> None:
        await self._recover_running()
        while not self._stopping:
            task_id = await self._reserve_next()
            if task_id is None:
                await asyncio.sleep(self.settings.codex_poll_seconds)
                continue
            await self._invoke_codex(task_id)

    def stop(self) -> None:
        self._stopping = True

    async def _recover_running(self) -> None:
        async with self.session_factory() as session:
            from sqlalchemy import select

            from vps_proxy_manager.models import CodexTask

            tasks = (
                await session.scalars(
                    select(CodexTask).where(CodexTask.status == CodexTaskStatus.running)
                )
            ).all()
            for task in tasks:
                task.status = CodexTaskStatus.failed
                task.error_code = "codex_worker_restarted"
                task.message = "Codex Worker 重启，初始化未自动重放；请在 Telegram 中重试"
                task.finished_at = datetime.now(UTC)
                repo = Repository(session, self.secret_box)
                candidate = await repo.get_candidate(task.candidate_id)
                candidate.lifecycle = HostLifecycle.failed
                candidate.error_code = task.error_code
                candidate.message = task.message
            await session.commit()

    async def _reserve_next(self) -> int | None:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.next_codex_task()
            if task is None:
                return None
            task.status = CodexTaskStatus.running
            task.progress = 5
            task.started_at = datetime.now(UTC)
            task.message = "Codex Worker 已接收任务"
            candidate = await repo.get_candidate(task.candidate_id)
            candidate.lifecycle = HostLifecycle.provisioning
            candidate.message = "Codex 正在初始化目标 VPS"
            await session.commit()
            return task.id

    async def _invoke_codex(self, task_id: int) -> None:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.get_codex_task(task_id)
            candidate_id = task.candidate_id
        output_dir = self.settings.data_dir / "codex-results"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_path = output_dir / f"task-{task_id}.txt"
        prompt = (
            "Use $vps-proxy-target-bootstrap to initialize the pending authorized VPS. "
            f"The Codex task id is {task_id} and candidate id is {candidate_id}. "
            "Do not use Telegram text as a shell command and do not inspect or print credentials. "
            "Run exactly the deterministic provisioning command documented by the skill, inspect its "
            "sanitized JSON result, and report whether admission succeeded."
        )
        command = [
            self.settings.codex_cli,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            "-C",
            str(self.settings.codex_work_dir),
            "-o",
            str(output_path),
            "-",
        ]
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.settings.codex_home)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self.settings.codex_timeout_seconds,
            )
        except TimeoutError:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            await self._fail(task_id, "codex_timeout", "Codex 初始化任务超时")
            return
        except OSError as exc:
            await self._fail(task_id, "codex_unavailable", f"无法启动 Codex CLI：{exc}")
            return
        if process.returncode != 0:
            detail = redact_text(stderr.decode("utf-8", errors="replace")[-800:])
            log.error("codex_exec_failed", task_id=task_id, detail=detail)
            await self._fail(task_id, "codex_exec_failed", "Codex CLI 执行失败")
            return
        needs_failure = False
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.get_codex_task(task_id)
            if task.status == CodexTaskStatus.running:
                needs_failure = True
        if needs_failure:
            await self._fail(
                task_id,
                "codex_no_result",
                "Codex 已结束，但没有执行受控初始化入口",
            )

    async def _fail(self, task_id: int, code: str, message: str) -> None:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.get_codex_task(task_id)
            if task.status == CodexTaskStatus.succeeded:
                return
            task.status = CodexTaskStatus.failed
            task.error_code = code
            task.message = message
            task.progress = 100
            task.finished_at = datetime.now(UTC)
            candidate = await repo.get_candidate(task.candidate_id)
            candidate.lifecycle = HostLifecycle.failed
            candidate.error_code = code
            candidate.message = message
            await session.commit()


async def provision_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
    ssh: SSHClient,
    *,
    candidate_id: int,
    codex_task_id: int,
) -> dict[str, Any]:
    async with session_factory() as session:
        repo = Repository(session, secret_box)
        task = await repo.get_codex_task(codex_task_id)
        candidate = await repo.get_candidate(candidate_id)
        if task.candidate_id != candidate.id:
            raise ValueError("Codex task does not match candidate")
        if task.status != CodexTaskStatus.running:
            raise ValueError("Codex task is not in running state")
        task.progress = 20
        task.message = "Codex 已调用受控初始化入口"
        candidate.lifecycle = HostLifecycle.provisioning
        await session.commit()
        secret = repo.decrypt_candidate_secret(candidate)
        creds = credentials_from_candidate(candidate, secret)
        try:
            initialized = await ssh.run_payload(creds, "initialize", {}, timeout=600)
            task.progress = 75
            task.message = "远端 Agent 已安装，正在进行准入验证"
            await session.commit()
            status_result = await ssh.run_agent(creds, "status", {}, timeout=60)
            status = status_result.get("status", {})
            if status.get("agent_version") != initialized.get("agent_version"):
                raise SSHError("agent_verification_failed", "远端 Agent 版本验证失败")
            if not status.get("singbox_version"):
                raise SSHError("singbox_verification_failed", "sing-box 安装验证失败")
            if status.get("singbox_active") == "active":
                raise SSHError("local_exit_verification_failed", "初始化后代理服务未保持停止状态")
            if not status.get("connectivity_ok"):
                raise SSHError(
                    "local_exit_verification_failed", "初始化后 VPS 本地公网访问验证失败"
                )
            candidate.system_info = initialized.get("system", candidate.system_info)
            host = await repo.promote_candidate(
                candidate,
                agent_version=str(initialized.get("agent_version") or "unknown"),
            )
            task.status = CodexTaskStatus.succeeded
            task.progress = 100
            task.message = f"初始化及验证通过，VPS #{host.id} 已正式入库"
            task.result = redact_obj(
                {
                    "host_id": host.id,
                    "agent_version": initialized.get("agent_version"),
                    "singbox_version": status.get("singbox_version"),
                    "exit_mode": "local",
                    "has_backup": status.get("has_backup"),
                }
            )
            task.finished_at = datetime.now(UTC)
            await repo.audit(
                actor_user_id=0,
                action="codex_provision",
                result="ok",
                host_id=host.id,
                detail={"codex_task_id": task.id, "candidate_id": candidate.id},
            )
            await session.commit()
            return {"ok": True, **task.result}
        except Exception as exc:
            await session.rollback()
            task = await repo.get_codex_task(codex_task_id)
            candidate = await repo.get_candidate(candidate_id)
            code = exc.code if isinstance(exc, SSHError) else "provision_failed"
            task.status = CodexTaskStatus.failed
            task.progress = 100
            task.error_code = code
            task.message = str(exc)[:500]
            task.result = {}
            task.finished_at = datetime.now(UTC)
            candidate.lifecycle = HostLifecycle.failed
            candidate.error_code = code
            candidate.message = task.message
            await session.commit()
            return {"ok": False, "error_code": code, "message": task.message}
