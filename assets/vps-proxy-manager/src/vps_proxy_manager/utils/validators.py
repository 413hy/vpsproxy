from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9_-]{1,63}\.)*[a-zA-Z0-9_-]{1,63}\.?$")
NAME_RE = re.compile(r"^[\w .:@+-]{1,80}$", re.UNICODE)
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def validate_name(value: str) -> str:
    value = value.strip()
    if not NAME_RE.fullmatch(value):
        raise ValueError("名称只能包含常见文字、数字、空格和 .:@+-，最长 80 字符")
    return value


def validate_host(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not HOST_RE.fullmatch(value):
        raise ValueError("IP 或域名格式不正确")
    return value.rstrip(".")


def validate_port(value: int | str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1-65535 之间")
    return port


def validate_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("SSH 用户名格式不安全")
    return value


def validate_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("订阅链接必须是 https URL")
    if parsed.username or parsed.password:
        raise ValueError("订阅 URL 不允许包含用户名或密码")
    return value.strip()
