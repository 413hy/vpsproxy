from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import aiohttp

BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
METADATA_IPS = {ipaddress.ip_address("169.254.169.254")}


class SSRFError(ValueError):
    pass


def _is_blocked_ip(ip: ipaddress._BaseAddress, allow_private: bool) -> bool:  # noqa: SLF001
    if ip in METADATA_IPS:
        return True
    if allow_private:
        return False
    return any(ip in network for network in BLOCKED_NETWORKS)


async def resolve_public_host(host: str, allow_private: bool) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip, allow_private):
            raise SSRFError(f"subscription host resolves to blocked address: {ip}")
        if family in {socket.AF_INET, socket.AF_INET6}:
            ips.append(str(ip))
    if not ips:
        raise SSRFError("subscription host cannot be resolved")
    return ips


async def fetch_subscription(
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int,
    max_redirects: int,
    allow_private: bool,
) -> str:
    current = url
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SSRFError("subscription URL must be https")
        if parsed.username or parsed.password:
            raise SSRFError("subscription URL must not include credentials")
        await resolve_public_host(parsed.hostname, allow_private)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(current, allow_redirects=False) as resp:
                if 300 <= resp.status < 400 and resp.headers.get("Location"):
                    current = urljoin(current, resp.headers["Location"])
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > max_bytes:
                        raise SSRFError("subscription response is too large")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
    raise SSRFError("too many subscription redirects")
