from __future__ import annotations

import pytest

from tests.test_parser import VLESS_REALITY
from vps_proxy_manager.crypto import SecretBox, generate_key
from vps_proxy_manager.db import create_engine, create_sessionmaker
from vps_proxy_manager.models import AuthMethod, Base
from vps_proxy_manager.proxy.parser import parse_node_link
from vps_proxy_manager.services.repository import Repository


@pytest.mark.asyncio
async def test_single_nodes_and_subscription_entries_stay_separate() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    secret_box = SecretBox(generate_key())
    spec = parse_node_link(VLESS_REALITY)

    async with factory() as session:
        repo = Repository(session, secret_box)
        await repo.save_nodes([spec])
        sub = await repo.create_subscription(
            "subscription", "https://example.com/sub", VLESS_REALITY, [spec]
        )
        await session.commit()

    async with factory() as session:
        repo = Repository(session, secret_box)
        assert await repo.count_nodes() == 1
        assert await repo.count_subscriptions() == 1
        entries = await repo.list_subscription_entries(sub.id)
        assert len(entries) == 1
        assert entries[0].subscription_id == sub.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_vps_receives_independent_node_and_subscription_copies() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_sessionmaker(engine)
    secret_box = SecretBox(generate_key())
    spec = parse_node_link(VLESS_REALITY)

    async with factory() as session:
        repo = Repository(session, secret_box)
        node = (await repo.save_nodes([spec]))[0]
        sub = await repo.create_subscription(
            "subscription", "https://example.com/sub", VLESS_REALITY, [spec]
        )
        host = await repo.add_host(
            name="target",
            host="203.0.113.20",
            port=22,
            username="root",
            auth_method=AuthMethod.password,
            secret="test-only",  # noqa: S106
            known_host="known-host",
        )
        vps_node = await repo.assign_node(host, node)
        vps_sub = await repo.assign_subscription(host, sub)
        await session.commit()

        assert vps_node.source_node_id == node.id
        assert vps_sub.source_subscription_id == sub.id
        assert repo.decrypt_node_link(vps_node) == VLESS_REALITY
        assert repo.decrypt_subscription_url(vps_sub) == "https://example.com/sub"

    await engine.dispose()
