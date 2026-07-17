from __future__ import annotations

import pytest

from vps_proxy_manager.models import AuthMethod
from vps_proxy_manager.ssh.client import SSHClient, SSHCredentials, SSHError
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


@pytest.mark.asyncio
async def test_remote_action_allowlist_rejects_command_injection_before_connect() -> None:
    credentials = SSHCredentials(
        host="example.com",
        port=22,
        username="root",
        auth_method=AuthMethod.password,
        secret="test-only",  # noqa: S106
    )
    with pytest.raises(SSHError, match="允许列表"):
        await SSHClient().run_agent(credentials, "status; reboot", {})


class FakeConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def run(self, command: str, **_kwargs: object) -> object:
        self.commands.append(command)
        return type(
            "Result",
            (),
            {"exit_status": 0, "stdout": '{"ok": true}', "stderr": ""},
        )()


class RecordingSSH(SSHClient):
    def __init__(self) -> None:
        self.connection = FakeConnection()

    async def connect(self, _creds: SSHCredentials):
        return self.connection


@pytest.mark.asyncio
async def test_root_target_does_not_require_sudo_binary() -> None:
    ssh = RecordingSSH()
    credentials = SSHCredentials(
        host="example.com",
        port=22,
        username="root",
        auth_method=AuthMethod.password,
        secret="test-only",  # noqa: S106
    )
    await ssh.run_agent(credentials, "status", {})
    assert ssh.connection.commands == ["/usr/local/sbin/vpspm-agent status"]


@pytest.mark.asyncio
async def test_non_root_target_uses_noninteractive_sudo() -> None:
    ssh = RecordingSSH()
    credentials = SSHCredentials(
        host="example.com",
        port=22,
        username="deployer",
        auth_method=AuthMethod.password,
        secret="test-only",  # noqa: S106
    )
    await ssh.run_agent(credentials, "status", {})
    assert ssh.connection.commands == ["sudo -n /usr/local/sbin/vpspm-agent status"]
