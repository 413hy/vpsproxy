from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SECRET_KEYS = {
    "token",
    "bot_token",
    "password",
    "private_key",
    "secret",
    "secret_key",
    "uuid",
    "subscription_url",
    "node_url",
    "url",
}

URL_CREDENTIAL_RE = re.compile(r"(vless|vmess|trojan|ss|hysteria2)://[^ \n\r\t]+", re.I)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


def mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}...{value[-keep:]}"


def redact_text(value: str) -> str:
    value = TOKEN_RE.sub("[telegram-token-redacted]", value)
    value = URL_CREDENTIAL_RE.sub("[proxy-link-redacted]", value)
    value = UUID_RE.sub("[uuid-redacted]", value)
    return value


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if key_s.lower() in SECRET_KEYS:
                out[key_s] = "[redacted]"
            else:
                out[key_s] = redact_obj(item)
        return out
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(item) for item in value)
    return value
