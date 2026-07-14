from __future__ import annotations

import pytest

from vps_proxy_manager.proxy.ssrf import SSRFError, resolve_public_host


@pytest.mark.asyncio
async def test_localhost_blocked() -> None:
    with pytest.raises(SSRFError):
        await resolve_public_host("localhost", allow_private=False)
