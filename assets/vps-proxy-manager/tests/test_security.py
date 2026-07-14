from __future__ import annotations

import pytest

from vps_proxy_manager.utils.redact import redact_text
from vps_proxy_manager.utils.validators import validate_host, validate_username


def test_redacts_proxy_link_and_uuid() -> None:
    text = "vless://11111111-1111-4111-8111-111111111111@example.com:443#x"
    assert "vless://" not in redact_text(text)
    assert "11111111" not in redact_text(text)


def test_rejects_command_like_username() -> None:
    with pytest.raises(ValueError):
        validate_username("root;reboot")


def test_rejects_bad_host() -> None:
    with pytest.raises(ValueError):
        validate_host("example.com;curl bad")
