from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import (
    AuditLog,
    AuthMethod,
    NodeStatus,
    ProxyNode,
    Subscription,
    Task,
    TaskKind,
    VpsHost,
)
from vps_proxy_manager.proxy.parser import ProxyNodeSpec
from vps_proxy_manager.utils.redact import redact_obj
from vps_proxy_manager.utils.validators import (
    validate_host,
    validate_name,
    validate_port,
    validate_username,
)


class Repository:
    def __init__(self, session: AsyncSession, secret_box: SecretBox) -> None:
        self.session = session
        self.secret_box = secret_box

    async def add_host(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        auth_method: AuthMethod,
        secret: str,
        known_host: str | None,
        system_info: dict[str, Any] | None = None,
    ) -> VpsHost:
        item = VpsHost(
            name=validate_name(name),
            host=validate_host(host),
            port=validate_port(port),
            username=validate_username(username),
            auth_method=auth_method,
            encrypted_secret=self.secret_box.encrypt(secret),
            known_host=known_host,
            system_info=system_info or {},
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def list_hosts(self) -> Sequence[VpsHost]:
        return (await self.session.scalars(select(VpsHost).order_by(VpsHost.name))).all()

    async def get_host(self, host_id: int) -> VpsHost:
        host = await self.session.get(VpsHost, host_id)
        if host is None:
            raise KeyError("host not found")
        return host

    def decrypt_host_secret(self, host: VpsHost) -> str:
        return self.secret_box.decrypt(host.encrypted_secret)

    async def save_nodes(
        self, nodes: Sequence[ProxyNodeSpec], *, subscription_id: int | None = None
    ) -> list[ProxyNode]:
        saved: list[ProxyNode] = []
        for spec in nodes:
            existing = await self.session.scalar(
                select(ProxyNode).where(ProxyNode.fingerprint == spec.fingerprint)
            )
            if existing:
                existing.name = spec.name
                existing.server = spec.server
                existing.port = spec.port
                existing.protocol = spec.protocol
                existing.tags = spec.tags
                existing.encrypted_link = self.secret_box.encrypt(spec.link)
                saved.append(existing)
                continue
            node = ProxyNode(
                name=spec.name,
                protocol=spec.protocol,
                server=spec.server,
                port=spec.port,
                subscription_id=subscription_id,
                encrypted_link=self.secret_box.encrypt(spec.link),
                fingerprint=spec.fingerprint,
                tags=spec.tags,
            )
            self.session.add(node)
            saved.append(node)
        await self.session.flush()
        return saved

    async def list_nodes(self, *, search: str | None = None, limit: int = 10, offset: int = 0) -> Sequence[ProxyNode]:
        stmt = select(ProxyNode).order_by(ProxyNode.last_latency_ms.is_(None), ProxyNode.last_latency_ms, ProxyNode.name)
        if search:
            stmt = stmt.where(ProxyNode.name.ilike(f"%{search}%"))
        return (await self.session.scalars(stmt.limit(limit).offset(offset))).all()

    async def get_node(self, node_id: int) -> ProxyNode:
        node = await self.session.get(ProxyNode, node_id)
        if node is None:
            raise KeyError("node not found")
        return node

    def decrypt_node_link(self, node: ProxyNode) -> str:
        return self.secret_box.decrypt(node.encrypted_link)

    async def create_subscription(self, name: str, url: str) -> Subscription:
        sub = Subscription(name=validate_name(name), encrypted_url=self.secret_box.encrypt(url))
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def create_task(
        self,
        *,
        kind: TaskKind,
        actor_user_id: int,
        host_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(kind=kind, actor_user_id=actor_user_id, host_id=host_id, payload=redact_obj(payload or {}))
        self.session.add(task)
        await self.session.flush()
        return task

    async def recent_tasks(self, limit: int = 10) -> Sequence[Task]:
        return (await self.session.scalars(select(Task).order_by(desc(Task.created_at)).limit(limit))).all()

    async def audit(
        self,
        *,
        actor_user_id: int,
        action: str,
        result: str,
        host_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                result=result,
                host_id=host_id,
                detail=redact_obj(detail or {}),
            )
        )

    async def update_node_test(self, node: ProxyNode, result: dict[str, Any]) -> None:
        node.last_test = redact_obj(result)
        latency = result.get("latency_ms")
        node.last_latency_ms = int(latency) if latency is not None else None
        node.status = NodeStatus.online if result.get("proxy_ok") else NodeStatus.offline

    async def set_host_current_node(self, host: VpsHost, node: ProxyNode) -> None:
        host.current_node_id = node.id
        host.config_version += 1
