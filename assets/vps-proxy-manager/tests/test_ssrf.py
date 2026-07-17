from __future__ import annotations

import ipaddress

import pytest

from vps_proxy_manager.proxy.ssrf import (
    PinnedResolver,
    SSRFError,
    _is_blocked_ip,
    resolve_public_host,
)


@pytest.mark.asyncio
async def test_localhost_blocked() -> None:
    with pytest.raises(SSRFError):
        await resolve_public_host("localhost", allow_private=False)


def test_private_subscription_mode_never_allows_loopback_or_metadata() -> None:
    assert _is_blocked_ip(ipaddress.ip_address("127.0.0.1"), allow_private=True)
    assert _is_blocked_ip(ipaddress.ip_address("169.254.169.254"), allow_private=True)
    assert not _is_blocked_ip(ipaddress.ip_address("10.0.0.10"), allow_private=True)
    assert _is_blocked_ip(ipaddress.ip_address("10.0.0.10"), allow_private=False)


@pytest.mark.asyncio
async def test_pinned_resolver_rejects_hostname_changes() -> None:
    resolver = PinnedResolver("subscription.example", ["203.0.113.10"])
    with pytest.raises(OSError, match="hostname changed"):
        await resolver.resolve("metadata.example", 443)
