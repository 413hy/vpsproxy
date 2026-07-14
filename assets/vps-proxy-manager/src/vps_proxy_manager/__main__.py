from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from vps_proxy_manager.bot.app import BotDeps, build_application
from vps_proxy_manager.config import get_settings
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker, init_db
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
    """Create database tables."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)

    async def _run() -> None:
        engine = create_engine(settings.database_url)
        await init_db(engine)
        await engine.dispose()

    asyncio.run(_run())
    typer.echo("database initialized")


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
        await init_db(engine)
        await runner.start()

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
    data_dir = Path(settings.data_dir)
    if data_dir.exists() and (data_dir.stat().st_mode & 0o077):
        problems.append(f"{data_dir} permissions should not allow group/other access")
    if problems:
        for item in problems:
            typer.echo(f"FAIL: {item}")
        raise typer.Exit(code=1)
    typer.echo("OK")


if __name__ == "__main__":
    app()
