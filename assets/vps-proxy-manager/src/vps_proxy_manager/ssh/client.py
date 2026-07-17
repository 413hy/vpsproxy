from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh

from vps_proxy_manager.models import AuthMethod, VpsCandidate, VpsHost
from vps_proxy_manager.remote import payload as remote_payload

REMOTE_ACTIONS = {
    "detect",
    "initialize",
    "upgrade_agent",
    "store_node",
    "store_subscription",
    "remove_node",
    "remove_subscription",
    "fetch_subscription",
    "apply_proxy",
    "confirm_proxy",
    "rollback",
    "stop_proxy",
    "restore_proxy",
    "uninstall",
    "status",
    "speedtest",
}


class SSHError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


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


def credentials_from_candidate(candidate: VpsCandidate, secret: str) -> SSHCredentials:
    return SSHCredentials(
        host=candidate.host,
        port=candidate.port,
        username=candidate.username,
        auth_method=candidate.auth_method,
        secret=secret,
        known_host=candidate.known_host,
    )


class SSHClient:
    async def connect(self, creds: SSHCredentials) -> asyncssh.SSHClientConnection:
        known_hosts: str | None = None
        tmp_paths: list[Path] = []
        client_keys: list[str] | None = None
        password: str | None = None
        try:
            if creds.known_host:
                fd, name = tempfile.mkstemp(prefix="vpspm_known_hosts_")
                os.close(fd)
                Path(name).write_text(creds.known_host, encoding="utf-8")
                Path(name).chmod(0o600)
                tmp_paths.append(Path(name))
                known_hosts = name
            if creds.auth_method == AuthMethod.password:
                password = creds.secret
            else:
                key_fd, key_name = tempfile.mkstemp(prefix="vpspm_key_")
                os.close(key_fd)
                Path(key_name).write_text(creds.secret, encoding="utf-8")
                Path(key_name).chmod(0o600)
                tmp_paths.append(Path(key_name))
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
            for tmp_path in tmp_paths:
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

    async def host_key_fingerprint(self, known_host: str) -> str:
        fd, name = tempfile.mkstemp(prefix="vpspm_fingerprint_")
        os.close(fd)
        path = Path(name)
        try:
            path.write_text(known_host, encoding="utf-8")
            path.chmod(0o600)
            proc = await asyncio.create_subprocess_exec(
                "ssh-keygen",
                "-lf",
                str(path),
                "-E",
                "sha256",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise SSHError(
                    "ssh_host_key_unverified",
                    stderr.decode("utf-8", errors="replace") or "无法计算 SSH 主机指纹",
                )
            fingerprints = []
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    fingerprints.append(parts[1])
            return ", ".join(dict.fromkeys(fingerprints))
        finally:
            path.unlink(missing_ok=True)

    async def run_payload(
        self,
        creds: SSHCredentials,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        sudo: bool = True,
        timeout: int = 120,
    ) -> dict[str, Any]:
        if action not in REMOTE_ACTIONS:
            raise SSHError("remote_action_denied", "远端操作不在允许列表中")
        source = Path(remote_payload.__file__).read_text(encoding="utf-8")
        source = source.rsplit('if __name__ == "__main__":', 1)[0]
        stdin = json.dumps(data or {}, ensure_ascii=False)
        privilege = "" if not sudo or creds.username == "root" else "sudo -n "
        cmd = f"{privilege}python3 - {action}"
        persisted_source = source + '\nif __name__ == "__main__":\n    main()\n'
        source_b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
        persisted_b64 = base64.b64encode(persisted_source.encode("utf-8")).decode("ascii")
        data_b64 = base64.b64encode(stdin.encode("utf-8")).decode("ascii")
        # Keep the SSH stdin wrapper ASCII-only so target locale settings cannot
        # corrupt non-ASCII messages embedded in the reviewed Agent source.
        script = (
            "import base64\n"
            f"source = base64.b64decode('{source_b64}').decode('utf-8')\n"
            f"persisted = base64.b64decode('{persisted_b64}').decode('utf-8')\n"
            f"data = base64.b64decode('{data_b64}').decode('utf-8')\n"
            "namespace = {'__name__': 'vpspm_payload', "
            "'_PAYLOAD_SOURCE': persisted, '_PAYLOAD_DATA': data}\n"
            "exec(compile(source, '<vpspm-agent>', 'exec'), namespace)\n"
            "namespace['main']()\n"
        )
        async with await self.connect(creds) as conn:
            result = await conn.run(cmd, input=script, timeout=timeout)
        return self._parse_result(result)

    async def run_agent(
        self,
        creds: SSHCredentials,
        action: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: int = 120,
    ) -> dict[str, Any]:
        if action not in REMOTE_ACTIONS - {"initialize"}:
            raise SSHError("remote_action_denied", "远端 Agent 操作不在允许列表中")
        stdin = json.dumps(data or {}, ensure_ascii=False)
        privilege = "" if creds.username == "root" else "sudo -n "
        async with await self.connect(creds) as conn:
            result = await conn.run(
                f"{privilege}/usr/local/sbin/vpspm-agent {action}",
                input=stdin,
                timeout=timeout,
            )
        return self._parse_result(result)

    def _parse_result(self, result: asyncssh.SSHCompletedProcess) -> dict[str, Any]:
        if result.exit_status != 0:
            stderr = (
                result.stderr.decode()
                if isinstance(result.stderr, bytes)
                else str(result.stderr or "")
            )
            raise SSHError("remote_payload_failed", stderr.strip() or "远端任务执行失败")
        try:
            stdout = (
                result.stdout.decode()
                if isinstance(result.stdout, bytes)
                else str(result.stdout or "")
            )
            parsed = json.loads(stdout.strip().splitlines()[-1])
        except Exception as exc:
            raise SSHError("remote_payload_bad_output", "远端返回结果格式错误") from exc
        if not parsed.get("ok", False):
            detail = str(parsed.get("detail") or "")
            raise SSHError(
                str(parsed.get("code", "remote_error")),
                str(parsed.get("message", "远端错误")),
                detail=detail,
            )
        return parsed
