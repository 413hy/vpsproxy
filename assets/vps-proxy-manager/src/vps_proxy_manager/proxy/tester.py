from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from vps_proxy_manager.proxy.parser import ProxyNodeSpec
from vps_proxy_manager.proxy.singbox import build_speedtest_config


class ProxyTestError(RuntimeError):
    pass


class LocalProxyTester:
    def __init__(self, *, attempts: int = 3) -> None:
        self.attempts = max(1, min(attempts, 5))

    async def test(self, node: ProxyNodeSpec) -> dict[str, Any]:
        singbox = shutil.which("sing-box")
        curl = shutil.which("curl")
        if not singbox or not curl:
            raise ProxyTestError("控制端需要安装 sing-box 和 curl 才能执行本地测速")
        port = self._free_port()
        config = build_speedtest_config(node, port)
        dns_result, tcp_result = await self._network_preflight(node)
        with tempfile.TemporaryDirectory(prefix="vpspm-local-test-") as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                singbox,
                "run",
                "-c",
                str(config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.sleep(1.2)
                if process.returncode is not None:
                    stderr = await process.stderr.read() if process.stderr else b""
                    raise ProxyTestError(
                        "测速代理实例启动失败：" + stderr.decode("utf-8", errors="replace")[-600:]
                    )
                samples: list[dict[str, int]] = []
                error = ""
                for _ in range(self.attempts):
                    sample, error = await self._curl_once(curl, port)
                    if sample:
                        samples.append(sample)
                proxy_ok = bool(samples)
                handshake = (
                    int(sum(item["proxy_handshake_ms"] for item in samples) / len(samples))
                    if samples
                    else None
                )
                access = (
                    int(sum(item["access_latency_ms"] for item in samples) / len(samples))
                    if samples
                    else None
                )
                return {
                    **dns_result,
                    **tcp_result,
                    "proxy_ok": proxy_ok,
                    "proxy_handshake_ms": handshake,
                    "access_latency_ms": access,
                    "latency_ms": access,
                    "attempts": self.attempts,
                    "successful_attempts": len(samples),
                    "samples": samples,
                    "test_url": "https://www.gstatic.com/generate_204",
                    "error": "" if proxy_ok else error or "proxy test failed",
                }
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()

    async def _network_preflight(
        self, node: ProxyNodeSpec
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        try:
            addresses = await asyncio.wait_for(
                loop.getaddrinfo(node.server, node.port, type=socket.SOCK_STREAM),
                timeout=5,
            )
            dns_ms = int((time.monotonic() - started) * 1000)
            address = addresses[0][4][0]
        except Exception as exc:  # noqa: BLE001
            return (
                {"dns_ok": False, "dns_latency_ms": None},
                {"tcp_ok": False, "tcp_latency_ms": None, "preflight_error": str(exc)},
            )
        tcp_started = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, node.port), timeout=5
            )
            writer.close()
            await writer.wait_closed()
            return (
                {"dns_ok": True, "dns_latency_ms": dns_ms},
                {"tcp_ok": True, "tcp_latency_ms": int((time.monotonic() - tcp_started) * 1000)},
            )
        except Exception as exc:  # noqa: BLE001
            return (
                {"dns_ok": True, "dns_latency_ms": dns_ms},
                {"tcp_ok": False, "tcp_latency_ms": None, "preflight_error": str(exc)},
            )

    async def _curl_once(self, curl: str, port: int) -> tuple[dict[str, int] | None, str]:
        process = await asyncio.create_subprocess_exec(
            curl,
            "--noproxy",
            "",
            "-x",
            f"http://127.0.0.1:{port}",
            "-fsS",
            "-o",
            "/dev/null",
            "-w",
            "%{time_starttransfer} %{time_total}",
            "--max-time",
            "15",
            "https://www.gstatic.com/generate_204",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None, "curl timeout"
        if process.returncode != 0:
            return None, stderr.decode("utf-8", errors="replace").strip()
        try:
            handshake_s, total_s = stdout.decode("ascii").strip().split()
            return {
                "proxy_handshake_ms": int(float(handshake_s) * 1000),
                "access_latency_ms": int(float(total_s) * 1000),
            }, ""
        except (ValueError, TypeError):
            return None, "curl timing output invalid"

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
