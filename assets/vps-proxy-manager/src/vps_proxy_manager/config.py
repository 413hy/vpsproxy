from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VPSPM_",
        env_file=(".env", "/etc/vps-proxy-manager/vps-proxy-manager.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(min_length=20)
    admin_user_ids: Annotated[list[int], NoDecode]
    allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    require_private_chat: bool = True
    database_url: str = "sqlite+aiosqlite:////opt/vps-proxy-manager/data/app.db"
    secret_key: str = Field(min_length=32)
    log_level: str = "INFO"
    data_dir: Path = Path("/opt/vps-proxy-manager/data")
    allow_private_subscription_urls: bool = False
    subscription_max_bytes: int = 1_048_576
    subscription_timeout_seconds: int = 12
    subscription_max_redirects: int = 3
    speedtest_concurrency: int = 3
    remote_rollback_seconds: int = 120
    codex_enabled: bool = True
    codex_cli: str = "codex"
    codex_home: Path = Path("/root/.codex")
    codex_work_dir: Path = Path("/opt/vps-proxy-manager")
    codex_poll_seconds: int = 3
    codex_timeout_seconds: int = 900

    @field_validator("admin_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def parse_int_list(cls, value: object) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [int(item) for item in value]
        raise TypeError("expected comma separated integers")

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("invalid log level")
        return level


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
