from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh

from vps_proxy_manager.models import AuthMethod, VpsHost
from vps_proxy_manager.remote import payload as remote_payload


class SSHError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SSHCredentials:
    host: str
    port: int
    username: str
    auth_method: AuthMethod
    secret: str
    known_host: str | None = None


def credentials_from_host(host: VpsHost, secret: str) -> SSHCredentials:
    return SSHCredentials(
        host=host.host,
        port=host.port,
        username=host.username,
        auth_method=host.auth_method,
        secret=secret,
        known_host=host.known_host,
    )


class SSHClient:
    async def connect(self, creds: SSHCredentials) -> asyncssh.SSHClientConnection:
        known_hosts: str | None = None
        tmp_path: Path | None = None
        client_keys: list[str] | None = None
        password: str | None = None
        try:
            if creds.known_host:
                fd, name = tempfile.mkstemp(prefix="vpspm_known_hosts_")
                os.close(fd)
                Path(name).write_text(creds.known_host, encoding="utf-8")
                Path(name).chmod(0o600)
                tmp_path = Path(name)
                known_hosts = name
            if creds.auth_method == AuthMethod.password:
                password = creds.secret
            else:
                key_fd, key_name = tempfile.mkstemp(prefix="vpspm_key_")
                os.close(key_fd)
                Path(key_name).write_text(creds.secret, encoding="utf-8")
                Path(key_name).chmod(0o600)
                tmp_path = Path(key_name)
                client_keys = [key_name]
            return await asyncssh.connect(
                creds.host,
                port=creds.port,
                username=creds.username,
                password=password,
                client_keys=client_keys,
                known_hosts=known_hosts,
                server_host_key_algs=["ssh-ed25519", "rsa-sha2-512", "rsa-sha2-256"],
                connect_timeout=15,
            )
        except asyncssh.PermissionDenied as exc:
            raise SSHError("ssh_auth_failed", "SSH 认证失败") from exc
        except asyncssh.HostKeyNotVerifiable as exc:
            raise SSHError("ssh_host_key_unverified", "SSH 主机指纹未验证") from exc
        except (OSError, asyncssh.Error) as exc:
            raise SSHError("ssh_connect_failed", f"SSH 连接失败: {exc}") from exc
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    async def capture_host_key(self, creds: SSHCredentials) -> str:
        proc = await asyncio.create_subprocess_exec(
            "ssh-keyscan",
            "-p",
            str(creds.port),
            "-T",
            "10",
            creds.host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not stdout.strip():
            raise SSHError(
                "ssh_host_key_unverified",
                stderr.decode("utf-8", errors="replace") or "无法获取 SSH 主机指纹",
            )
        return stdout.decode("utf-8")

    async def run_payload(
        self,
        creds: SSHCredentials,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        sudo: bool = True,
        timeout: int = 120,
    ) -> dict[str, Any]:
        source = Path(remote_payload.__file__).read_text(encoding="utf-8")
        source = source.rsplit('if __name__ == "__main__":', 1)[0]
        stdin = json.dumps(data or {}, ensure_ascii=False)
        cmd = f"{'sudo ' if sudo else ''}python3 - {action}"
        script = source + "\n_PAYLOAD_DATA = " + repr(stdin) + "\nmain()\n"
        async with await self.connect(creds) as conn:
            result = await conn.run(cmd, input=script, timeout=timeout)
        if result.exit_status != 0:
            stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else str(result.stderr or "")
            raise SSHError("remote_payload_failed", stderr.strip() or "远端任务执行失败")
        try:
            stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else str(result.stdout or "")
            parsed = json.loads(stdout.strip().splitlines()[-1])
        except Exception as exc:
            raise SSHError("remote_payload_bad_output", "远端返回结果格式错误") from exc
        if not parsed.get("ok", False):
            raise SSHError(str(parsed.get("code", "remote_error")), str(parsed.get("message", "远端错误")))
        return parsed
