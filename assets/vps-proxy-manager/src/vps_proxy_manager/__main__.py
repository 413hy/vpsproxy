from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from vps_proxy_manager.bot.app import BotDeps, build_application, configure_bot
from vps_proxy_manager.codex.worker import CodexWorker, provision_candidate
from vps_proxy_manager.config import get_settings
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.logging import configure_logging
from vps_proxy_manager.ssh.client import SSHClient
from vps_proxy_manager.tasks.runner import TaskRunner

app = typer.Typer(no_args_is_help=True)


@app.command("keygen")
def keygen() -> None:
    """Generate a Fernet key for VPSPM_SECRET_KEY."""
    typer.echo(generate_key())


@app.command("init-db")
def init_database() -> None:
    """Create or migrate the database to the current schema."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)

    async def _existing_tables() -> set[str]:
        engine = create_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
                return set(names)
        finally:
            await engine.dispose()

    tables = asyncio.run(_existing_tables())
    project_dir = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_dir / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_dir / "migrations"))
    if "vps_hosts" in tables and "alembic_version" not in tables:
        command.stamp(alembic_config, "0001_initial")
    command.upgrade(alembic_config, "head")
    typer.echo("database migrated")


@app.command("run-codex-worker")
def run_codex_worker() -> None:
    """Run the controlled Codex provisioning bridge."""
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.codex_enabled:
        typer.echo("Codex worker is disabled")
        raise typer.Exit(code=0)
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    worker = CodexWorker(session_factory, settings, SecretBox(settings.secret_key))

    async def _run() -> None:
        try:
            await worker.run_forever()
        finally:
            await engine.dispose()

    asyncio.run(_run())


@app.command("provision-candidate")
def provision_candidate_command(
    candidate_id: int = typer.Option(..., min=1),
    codex_task_id: int = typer.Option(..., min=1),
) -> None:
    """Provision one candidate through the deterministic Codex-only entrypoint."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)

    async def _run() -> dict[str, object]:
        try:
            return await provision_candidate(
                session_factory,
                SecretBox(settings.secret_key),
                SSHClient(),
                candidate_id=candidate_id,
                codex_task_id=codex_task_id,
            )
        finally:
            await engine.dispose()

    typer.echo(json.dumps(asyncio.run(_run()), ensure_ascii=False))


@app.command("run-bot")
def run_bot() -> None:
    """Run Telegram bot polling loop."""
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    secret_box = SecretBox(settings.secret_key)
    runner = TaskRunner(session_factory, settings, secret_box)
    tg_app = build_application(
        BotDeps(
            settings=settings,
            session_factory=session_factory,
            secret_box=secret_box,
            runner=runner,
            ssh=SSHClient(),
        )
    )

    async def post_init(_: object) -> None:
        await runner.start()
        await configure_bot(tg_app)

    async def post_shutdown(_: object) -> None:
        await runner.stop()
        await engine.dispose()

    tg_app.post_init = post_init
    tg_app.post_shutdown = post_shutdown
    tg_app.run_polling(allowed_updates=["message", "callback_query"])


@app.command("doctor")
def doctor() -> None:
    """Run local sanity checks without printing secrets."""
    settings = get_settings()
    problems: list[str] = []
    if not settings.admin_user_ids:
        problems.append("VPSPM_ADMIN_USER_IDS is empty")
    if settings.telegram_bot_token.startswith("replace"):
        problems.append("VPSPM_TELEGRAM_BOT_TOKEN is not configured")
    if settings.secret_key.startswith("replace"):
        problems.append("VPSPM_SECRET_KEY is not configured")
    else:
        try:
            SecretBox(settings.secret_key)
        except (TypeError, ValueError):
            problems.append("VPSPM_SECRET_KEY is not a valid Fernet key")
    data_dir = Path(settings.data_dir)
    if data_dir.exists() and (data_dir.stat().st_mode & 0o077):
        problems.append(f"{data_dir} permissions should not allow group/other access")
    if settings.codex_enabled:
        codex_path = (
            str(Path(settings.codex_cli))
            if Path(settings.codex_cli).is_absolute() and Path(settings.codex_cli).exists()
            else shutil.which(settings.codex_cli)
        )
        if not codex_path:
            problems.append(f"Codex CLI not found: {settings.codex_cli}")
        else:
            try:
                login = subprocess.run(
                    [codex_path, "login", "status"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "CODEX_HOME": str(settings.codex_home)},
                )
                if login.returncode != 0:
                    problems.append("Codex CLI is not logged in")
            except (OSError, subprocess.TimeoutExpired):
                problems.append("Codex login status check failed")
        if not settings.codex_home.exists():
            problems.append(f"Codex home not found: {settings.codex_home}")
    if problems:
        for item in problems:
            typer.echo(f"FAIL: {item}")
        raise typer.Exit(code=1)
    typer.echo("OK")


if __name__ == "__main__":
    app()
