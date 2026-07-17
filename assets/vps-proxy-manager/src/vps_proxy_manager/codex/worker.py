from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from vps_proxy_manager.config import Settings
from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import CodexTask, CodexTaskStatus, HostLifecycle, Task
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
            try:
                await self._invoke_codex(task_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("codex_worker_task_failed", task_id=task_id, error=str(exc))
                try:
                    await self._fail(
                        task_id,
                        "codex_worker_internal_error",
                        "Codex Worker 内部错误，诊断任务已停止",
                    )
                except Exception as fail_exc:  # noqa: BLE001
                    log.error(
                        "codex_worker_failure_record_failed",
                        task_id=task_id,
                        error=redact_text(str(fail_exc)),
                    )

    def stop(self) -> None:
        self._stopping = True

    async def _recover_running(self) -> None:
        async with self.session_factory() as session:
            tasks = (
                await session.scalars(
                    select(CodexTask).where(CodexTask.status == CodexTaskStatus.running)
                )
            ).all()
            for task in tasks:
                task.status = CodexTaskStatus.failed
                task.error_code = "codex_worker_restarted"
                task.message = "Codex Worker 重启，任务未自动重放"
                task.finished_at = datetime.now(UTC)
                if task.operation == "provision" and task.candidate_id is not None:
                    repo = Repository(session, self.secret_box)
                    candidate = await repo.get_candidate(task.candidate_id)
                    candidate.lifecycle = HostLifecycle.failed
                    candidate.error_code = task.error_code
                    candidate.message = task.message
                elif task.source_task_id is not None:
                    source = await session.get(Task, task.source_task_id)
                    if source is not None:
                        source.result = {
                            **(source.result or {}),
                            "codex_diagnostic": {
                                "task_id": task.id,
                                "status": "failed",
                                "message": task.message,
                            },
                        }
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
            if task.operation == "provision":
                if task.candidate_id is None:
                    task.status = CodexTaskStatus.failed
                    task.error_code = "invalid_codex_task"
                    task.message = "初始化任务缺少候选 VPS"
                    task.finished_at = datetime.now(UTC)
                    await session.commit()
                    return None
                candidate = await repo.get_candidate(task.candidate_id)
                candidate.lifecycle = HostLifecycle.provisioning
                candidate.message = "Codex 正在初始化目标 VPS"
            elif task.operation == "diagnose":
                if (
                    task.source_task_id is None
                    or await session.get(Task, task.source_task_id) is None
                ):
                    task.status = CodexTaskStatus.failed
                    task.error_code = "diagnostic_source_missing"
                    task.message = "待诊断的后台任务不存在"
                    task.finished_at = datetime.now(UTC)
                    await session.commit()
                    return None
                task.message = "Codex 正在自动分析失败任务"
            else:
                task.status = CodexTaskStatus.failed
                task.error_code = "unsupported_codex_operation"
                task.message = "不支持的 Codex 操作"
                task.finished_at = datetime.now(UTC)
                await session.commit()
                return None
            await session.commit()
            task_id = task.id
            operation = task.operation
        if operation == "diagnose":
            await self._notify_diagnosis(task_id, started=True)
        return task_id

    async def _invoke_codex(self, task_id: int) -> None:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.get_codex_task(task_id)
            operation = task.operation
        if operation == "diagnose":
            await self._invoke_diagnosis(task_id)
        else:
            await self._invoke_provision(task_id)

    async def _invoke_provision(self, task_id: int) -> None:
        async with self.session_factory() as session:
            task = await Repository(session, self.secret_box).get_codex_task(task_id)
            candidate_id = task.candidate_id
        if candidate_id is None:
            await self._fail(task_id, "invalid_codex_task", "初始化任务缺少候选 VPS")
            return
        output_dir = self.settings.data_dir / "codex-results"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_path = output_dir / f"task-{task_id}.txt"
        output_path.unlink(missing_ok=True)
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
            "--model",
            self.settings.codex_model,
            "-c",
            'approval_policy="never"',
            "-c",
            f'model_reasoning_effort="{self.settings.codex_reasoning_effort}"',
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

    async def _invoke_diagnosis(self, task_id: int) -> None:
        output_dir = self.settings.data_dir / "codex-results"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        context_path = output_dir / f"diagnosis-{task_id}-context.json"
        output_path = output_dir / f"diagnosis-{task_id}-result.json"
        output_path.unlink(missing_ok=True)
        try:
            context = await self._diagnostic_context(task_id)
            context_path.write_text(
                json.dumps(context, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            context_path.chmod(0o600)
        except (OSError, ValueError, KeyError) as exc:
            log.error(
                "codex_diagnosis_context_failed",
                task_id=task_id,
                error=redact_text(str(exc)),
            )
            await self._fail(task_id, "diagnostic_context_failed", "无法生成脱敏诊断上下文")
            return
        schema_path = Path(__file__).with_name("diagnosis_schema.json")
        prompt = (
            "Use $vps-proxy-task-diagnosis to analyze an automatically captured failure in this "
            "authorized VPS management system. "
            f"The Codex diagnostic task id is {task_id}. Read only the sanitized context file "
            f"{context_path} and relevant project source code. Do not inspect environment files, "
            "credentials, databases, proxy URLs, or unrelated host files. Do not make changes or "
            "run remote/network/service commands. Return the structured diagnosis required by the "
            "output schema in Chinese."
        )
        command = [
            self.settings.codex_cli,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.settings.codex_model,
            "-c",
            'approval_policy="never"',
            "-c",
            f'model_reasoning_effort="{self.settings.codex_reasoning_effort}"',
            "--output-schema",
            str(schema_path),
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
            await self._fail(task_id, "codex_timeout", "Codex 自动诊断超时")
            return
        except OSError as exc:
            await self._fail(task_id, "codex_unavailable", f"无法启动 Codex CLI：{exc}")
            return
        if process.returncode != 0:
            detail = redact_text(stderr.decode("utf-8", errors="replace")[-800:])
            log.error("codex_diagnosis_failed", task_id=task_id, detail=detail)
            await self._fail(task_id, "codex_exec_failed", "Codex 自动诊断执行失败")
            return
        if not output_path.is_file():
            await self._fail(task_id, "codex_no_result", "Codex 没有返回诊断结果")
            return
        try:
            diagnosis = json.loads(output_path.read_text(encoding="utf-8")[:20000])
        except (OSError, ValueError):
            await self._fail(task_id, "codex_invalid_result", "Codex 返回了无效诊断结果")
            return
        if not isinstance(diagnosis, dict):
            await self._fail(task_id, "codex_invalid_result", "Codex 返回了无效诊断结构")
            return
        try:
            await self._complete_diagnosis(task_id, redact_obj(diagnosis))
        except (ValueError, KeyError) as exc:
            log.error(
                "codex_diagnosis_store_failed",
                task_id=task_id,
                error=redact_text(str(exc)),
            )
            await self._fail(task_id, "codex_result_store_failed", "无法保存 Codex 诊断结果")

    async def _diagnostic_context(self, task_id: int) -> dict[str, Any]:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            diagnostic = await repo.get_codex_task(task_id)
            if diagnostic.source_task_id is None:
                raise ValueError("diagnostic task has no source task")
            source = await session.get(Task, diagnostic.source_task_id)
            if source is None:
                raise ValueError("source task not found")
            host_context: dict[str, Any] | None = None
            if source.host_id is not None:
                try:
                    host = await repo.get_host(source.host_id)
                    host_context = {
                        "id": host.id,
                        "name": host.name,
                        "lifecycle": host.lifecycle.value,
                        "agent_version": host.remote_agent_version,
                        "system_info": host.system_info,
                        "last_status": host.last_status,
                    }
                except KeyError:
                    host_context = {"id": source.host_id, "state": "deleted_or_missing"}
            resource: dict[str, Any] | None = None
            if source.kind.value == "vps_subscription_test":
                subscription_id = source.payload.get("vps_subscription_id")
                if subscription_id:
                    try:
                        sub = await repo.get_vps_subscription(int(subscription_id))
                        resource = {
                            "type": "vps_subscription",
                            "id": sub.id,
                            "name": sub.name,
                            "node_count": sub.node_count,
                            "last_error": sub.last_error,
                            "last_update_at": sub.last_update_at,
                        }
                    except KeyError:
                        resource = {"type": "vps_subscription", "state": "missing"}
            return redact_obj(
                {
                    "schema_version": 1,
                    "diagnostic_task_id": diagnostic.id,
                    "source_task": {
                        "id": source.id,
                        "kind": source.kind.value,
                        "status": source.status.value,
                        "host_id": source.host_id,
                        "payload": source.payload,
                        "error_code": source.error_code,
                        "message": source.message,
                        "technical_result": source.result,
                        "created_at": source.created_at,
                        "started_at": source.started_at,
                        "finished_at": source.finished_at,
                    },
                    "host": host_context,
                    "resource": resource,
                    "relevant_source_files": [
                        "src/vps_proxy_manager/tasks/runner.py",
                        "src/vps_proxy_manager/db.py",
                        "src/vps_proxy_manager/ssh/client.py",
                        "src/vps_proxy_manager/remote/payload.py",
                    ],
                    "constraints": {
                        "analysis_only": True,
                        "no_automatic_retry": True,
                        "no_network_or_service_changes": True,
                    },
                }
            )

    async def _complete_diagnosis(self, task_id: int, diagnosis: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            repo = Repository(session, self.secret_box)
            task = await repo.get_codex_task(task_id)
            if task.source_task_id is None:
                raise ValueError("diagnostic task has no source task")
            source = await session.get(Task, task.source_task_id)
            if source is None:
                raise ValueError("source task not found")
            summary = str(diagnosis.get("summary") or "Codex 已完成自动诊断")[:500]
            task.status = CodexTaskStatus.succeeded
            task.progress = 100
            task.message = summary
            task.result = {
                "source_task_id": source.id,
                "model": self.settings.codex_model,
                "reasoning_effort": self.settings.codex_reasoning_effort,
                "diagnosis": diagnosis,
            }
            task.finished_at = datetime.now(UTC)
            source.result = {
                **(source.result or {}),
                "codex_diagnostic_task_id": task.id,
                "codex_diagnostic": {
                    "status": "succeeded",
                    "summary": summary,
                    "retry_safe": bool(diagnosis.get("retry_safe")),
                },
            }
            await repo.audit(
                actor_user_id=0,
                action="codex_diagnosis",
                result="ok",
                host_id=source.host_id,
                detail={"source_task_id": source.id, "codex_task_id": task.id},
            )
            await session.commit()
        await self._notify_diagnosis(task_id, started=False)

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
            if task.operation == "provision" and task.candidate_id is not None:
                candidate = await repo.get_candidate(task.candidate_id)
                candidate.lifecycle = HostLifecycle.failed
                candidate.error_code = code
                candidate.message = message
            elif task.source_task_id is not None:
                source = await session.get(Task, task.source_task_id)
                if source is not None:
                    source.result = {
                        **(source.result or {}),
                        "codex_diagnostic_task_id": task.id,
                        "codex_diagnostic": {
                            "status": "failed",
                            "message": message,
                            "error_code": code,
                        },
                    }
            await session.commit()
        if task.operation == "diagnose":
            await self._notify_diagnosis(task_id, started=False)

    async def _notify_diagnosis(self, task_id: int, *, started: bool) -> None:
        try:
            async with self.session_factory() as session:
                task = await Repository(session, self.secret_box).get_codex_task(task_id)
                if task.source_task_id is None:
                    return
                source = await session.get(Task, task.source_task_id)
                if source is None:
                    return
                targets = {source.actor_user_id, *self.settings.admin_user_ids}
                if started:
                    text = (
                        f"后台任务 #{source.id} 执行失败。\n"
                        f"Codex 已自动介入诊断（诊断任务 #{task.id}）。"
                    )
                elif task.status == CodexTaskStatus.succeeded:
                    diagnosis = task.result.get("diagnosis", {})
                    actions = diagnosis.get("recommended_actions", [])
                    action_text = "\n".join(f"• {item}" for item in actions[:4])
                    text = (
                        f"Codex 自动诊断 #{task.id} 已完成\n\n"
                        f"根因：{diagnosis.get('root_cause', task.message)}\n"
                        f"结论：{diagnosis.get('summary', task.message)}"
                        + (f"\n\n建议：\n{action_text}" if action_text else "")
                    )
                else:
                    text = f"Codex 自动诊断 #{task.id} 失败：{task.message}"
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("查看 Codex 诊断", callback_data=f"ct:v:{task_id}")],
                    [
                        InlineKeyboardButton(
                            "查看原任务", callback_data=f"t:v:{task.source_task_id}"
                        )
                    ],
                ]
            )
            async with Bot(self.settings.telegram_bot_token) as bot:
                for chat_id in targets:
                    if chat_id > 0:
                        await bot.send_message(
                            chat_id=chat_id, text=text[:4000], reply_markup=markup
                        )
        except TelegramError as exc:
            log.warning("codex_diagnosis_notification_failed", task_id=task_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "codex_diagnosis_notification_internal_error",
                task_id=task_id,
                error=redact_text(str(exc)),
            )


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
