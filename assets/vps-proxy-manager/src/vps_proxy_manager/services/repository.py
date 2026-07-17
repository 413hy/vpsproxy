from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vps_proxy_manager.crypto import SecretBox
from vps_proxy_manager.models import (
    AuditLog,
    AuthMethod,
    CodexTask,
    CodexTaskStatus,
    HostLifecycle,
    NodeStatus,
    ProxyMode,
    ProxyNode,
    Subscription,
    SubscriptionEntry,
    Task,
    TaskKind,
    VpsCandidate,
    VpsHost,
    VpsNode,
    VpsProxyState,
    VpsSubscription,
    VpsSubscriptionEntry,
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

    async def add_candidate(
        self,
        *,
        name: str,
        host: str,
        port: int,
        username: str,
        auth_method: AuthMethod,
        secret: str,
        known_host: str,
        system_info: dict[str, Any],
    ) -> VpsCandidate:
        validated_name = validate_name(name)
        existing_host = await self.session.scalar(
            select(VpsHost.id).where(VpsHost.name == validated_name)
        )
        existing_candidate = await self.session.scalar(
            select(VpsCandidate.id).where(
                VpsCandidate.name == validated_name,
                VpsCandidate.lifecycle != HostLifecycle.ready,
            )
        )
        if existing_host or existing_candidate:
            raise ValueError("VPS 名称已存在")
        item = VpsCandidate(
            name=validated_name,
            host=validate_host(host),
            port=validate_port(port),
            username=validate_username(username),
            auth_method=auth_method,
            encrypted_secret=self.secret_box.encrypt(secret),
            known_host=known_host,
            system_info=system_info,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_candidate(self, candidate_id: int) -> VpsCandidate:
        item = await self.session.get(VpsCandidate, candidate_id)
        if item is None:
            raise KeyError("candidate not found")
        return item

    async def list_candidates(self) -> Sequence[VpsCandidate]:
        stmt = (
            select(VpsCandidate)
            .where(VpsCandidate.lifecycle != HostLifecycle.ready)
            .order_by(desc(VpsCandidate.created_at))
        )
        return (await self.session.scalars(stmt)).all()

    def decrypt_candidate_secret(self, candidate: VpsCandidate) -> str:
        return self.secret_box.decrypt(candidate.encrypted_secret)

    async def promote_candidate(self, candidate: VpsCandidate, *, agent_version: str) -> VpsHost:
        existing = await self.session.scalar(select(VpsHost).where(VpsHost.name == candidate.name))
        if existing:
            raise ValueError("VPS 名称已存在")
        host = VpsHost(
            name=candidate.name,
            host=candidate.host,
            port=candidate.port,
            username=candidate.username,
            auth_method=candidate.auth_method,
            encrypted_secret=candidate.encrypted_secret,
            known_host=candidate.known_host,
            system_info=candidate.system_info,
            lifecycle=HostLifecycle.ready,
            remote_agent_version=agent_version,
            last_status={"exit_mode": "local", "singbox_active": "inactive"},
        )
        self.session.add(host)
        await self.session.flush()
        self.session.add(VpsProxyState(host_id=host.id, mode=ProxyMode.local))
        candidate.lifecycle = HostLifecycle.ready
        candidate.message = f"初始化成功，已加入 VPS 管理：#{host.id}"
        await self.session.flush()
        return host

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
            lifecycle=HostLifecycle.ready,
        )
        self.session.add(item)
        await self.session.flush()
        self.session.add(VpsProxyState(host_id=item.id, mode=ProxyMode.local))
        return item

    async def list_hosts(self) -> Sequence[VpsHost]:
        stmt = (
            select(VpsHost).where(VpsHost.lifecycle == HostLifecycle.ready).order_by(VpsHost.name)
        )
        return (await self.session.scalars(stmt)).all()

    async def get_host(self, host_id: int) -> VpsHost:
        host = await self.session.get(VpsHost, host_id)
        if host is None:
            raise KeyError("host not found")
        return host

    def decrypt_host_secret(self, host: VpsHost) -> str:
        return self.secret_box.decrypt(host.encrypted_secret)

    async def get_proxy_state(self, host_id: int) -> VpsProxyState:
        state = await self.session.get(VpsProxyState, host_id)
        if state is None:
            state = VpsProxyState(host_id=host_id, mode=ProxyMode.local)
            self.session.add(state)
            await self.session.flush()
        return state

    async def delete_host_record(self, host_id: int) -> None:
        await self.session.execute(update(Task).where(Task.host_id == host_id).values(host_id=None))
        await self.session.execute(delete(VpsHost).where(VpsHost.id == host_id))

    async def save_nodes(self, nodes: Sequence[ProxyNodeSpec]) -> list[ProxyNode]:
        saved: list[ProxyNode] = []
        for spec in nodes:
            existing = await self.session.scalar(
                select(ProxyNode).where(
                    ProxyNode.fingerprint == spec.fingerprint,
                    ProxyNode.subscription_id.is_(None),
                )
            )
            if existing:
                self._copy_spec(existing, spec)
                saved.append(existing)
                continue
            node = ProxyNode(
                name=spec.name,
                protocol=spec.protocol,
                server=spec.server,
                port=spec.port,
                subscription_id=None,
                encrypted_link=self.secret_box.encrypt(spec.link),
                fingerprint=spec.fingerprint,
                tags=spec.tags,
            )
            self.session.add(node)
            saved.append(node)
        await self.session.flush()
        return saved

    def _copy_spec(self, item: ProxyNode | VpsNode, spec: ProxyNodeSpec) -> None:
        item.name = spec.name
        item.server = spec.server
        item.port = spec.port
        item.protocol = spec.protocol
        item.encrypted_link = self.secret_box.encrypt(spec.link)
        if isinstance(item, ProxyNode):
            item.tags = spec.tags

    async def list_nodes(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        sort: str = "latency",
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[ProxyNode]:
        stmt = select(ProxyNode).where(ProxyNode.subscription_id.is_(None))
        if sort == "name":
            stmt = stmt.order_by(ProxyNode.name)
        else:
            stmt = stmt.order_by(
                ProxyNode.last_latency_ms.is_(None), ProxyNode.last_latency_ms, ProxyNode.name
            )
        if search:
            stmt = stmt.where(ProxyNode.name.ilike(f"%{search}%"))
        if status:
            stmt = stmt.where(ProxyNode.status == NodeStatus(status))
        return (await self.session.scalars(stmt.limit(limit).offset(offset))).all()

    async def count_nodes(self) -> int:
        stmt = (
            select(func.count()).select_from(ProxyNode).where(ProxyNode.subscription_id.is_(None))
        )
        return int(await self.session.scalar(stmt) or 0)

    async def get_node(self, node_id: int) -> ProxyNode:
        node = await self.session.get(ProxyNode, node_id)
        if node is None or node.subscription_id is not None:
            raise KeyError("node not found")
        return node

    def decrypt_node_link(
        self, node: ProxyNode | VpsNode | SubscriptionEntry | VpsSubscriptionEntry
    ) -> str:
        return self.secret_box.decrypt(node.encrypted_link)

    async def node_usage(self, node_id: int) -> Sequence[VpsNode]:
        stmt = select(VpsNode).where(VpsNode.source_node_id == node_id).order_by(VpsNode.host_id)
        return (await self.session.scalars(stmt)).all()

    async def delete_node(self, node_id: int) -> None:
        await self.session.execute(
            update(VpsHost).where(VpsHost.current_node_id == node_id).values(current_node_id=None)
        )
        await self.session.execute(delete(ProxyNode).where(ProxyNode.id == node_id))

    async def create_subscription(
        self, name: str, url: str, content: str, specs: Sequence[ProxyNodeSpec]
    ) -> Subscription:
        sub = Subscription(
            name=validate_name(name),
            encrypted_url=self.secret_box.encrypt(url),
            encrypted_content=self.secret_box.encrypt(content),
            node_count=len(specs),
            last_update_at=datetime.now(UTC),
        )
        self.session.add(sub)
        await self.session.flush()
        await self.replace_subscription_entries(sub, specs)
        return sub

    async def list_subscriptions(
        self, *, search: str | None = None, limit: int = 10, offset: int = 0
    ) -> Sequence[Subscription]:
        stmt = select(Subscription).order_by(Subscription.name)
        if search:
            stmt = stmt.where(Subscription.name.ilike(f"%{search}%"))
        stmt = stmt.limit(limit).offset(offset)
        return (await self.session.scalars(stmt)).all()

    async def count_subscriptions(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(Subscription)) or 0)

    async def get_subscription(self, subscription_id: int) -> Subscription:
        sub = await self.session.get(Subscription, subscription_id)
        if sub is None:
            raise KeyError("subscription not found")
        return sub

    def decrypt_subscription_url(self, sub: Subscription | VpsSubscription) -> str:
        return self.secret_box.decrypt(sub.encrypted_url)

    def decrypt_subscription_content(self, sub: Subscription | VpsSubscription) -> str:
        if not sub.encrypted_content:
            raise ValueError("subscription has no cached content")
        return self.secret_box.decrypt(sub.encrypted_content)

    async def update_subscription_content(
        self, sub: Subscription, content: str, specs: Sequence[ProxyNodeSpec]
    ) -> None:
        sub.encrypted_content = self.secret_box.encrypt(content)
        sub.node_count = len(specs)
        sub.last_update_at = datetime.now(UTC)
        sub.last_error = None
        await self.replace_subscription_entries(sub, specs)

    async def replace_subscription_entries(
        self, sub: Subscription, specs: Sequence[ProxyNodeSpec]
    ) -> list[SubscriptionEntry]:
        current = {
            item.fingerprint: item
            for item in (
                await self.session.scalars(
                    select(SubscriptionEntry).where(SubscriptionEntry.subscription_id == sub.id)
                )
            ).all()
        }
        seen: set[str] = set()
        saved: list[SubscriptionEntry] = []
        for spec in specs:
            seen.add(spec.fingerprint)
            item = current.get(spec.fingerprint)
            if item is None:
                item = SubscriptionEntry(
                    subscription_id=sub.id,
                    name=spec.name,
                    protocol=spec.protocol,
                    server=spec.server,
                    port=spec.port,
                    fingerprint=spec.fingerprint,
                    encrypted_link=self.secret_box.encrypt(spec.link),
                )
                self.session.add(item)
            else:
                item.name = spec.name
                item.protocol = spec.protocol
                item.server = spec.server
                item.port = spec.port
                item.encrypted_link = self.secret_box.encrypt(spec.link)
            saved.append(item)
        stale = set(current) - seen
        if stale:
            await self.session.execute(
                delete(SubscriptionEntry).where(
                    SubscriptionEntry.subscription_id == sub.id,
                    SubscriptionEntry.fingerprint.in_(stale),
                )
            )
        await self.session.flush()
        return saved

    async def list_subscription_entries(
        self,
        subscription_id: int,
        *,
        search: str | None = None,
        status: str | None = None,
        sort: str = "latency",
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[SubscriptionEntry]:
        stmt = select(SubscriptionEntry).where(SubscriptionEntry.subscription_id == subscription_id)
        if search:
            stmt = stmt.where(SubscriptionEntry.name.ilike(f"%{search}%"))
        if status:
            stmt = stmt.where(SubscriptionEntry.status == NodeStatus(status))
        if sort == "name":
            stmt = stmt.order_by(SubscriptionEntry.name)
        else:
            stmt = stmt.order_by(
                SubscriptionEntry.last_latency_ms.is_(None),
                SubscriptionEntry.last_latency_ms,
                SubscriptionEntry.name,
            )
        stmt = stmt.limit(limit).offset(offset)
        return (await self.session.scalars(stmt)).all()

    async def get_subscription_entry(self, entry_id: int) -> SubscriptionEntry:
        item = await self.session.get(SubscriptionEntry, entry_id)
        if item is None:
            raise KeyError("subscription entry not found")
        return item

    async def subscription_usage(self, subscription_id: int) -> Sequence[VpsSubscription]:
        stmt = select(VpsSubscription).where(
            VpsSubscription.source_subscription_id == subscription_id
        )
        return (await self.session.scalars(stmt)).all()

    async def delete_subscription(self, subscription_id: int) -> None:
        await self.session.execute(delete(Subscription).where(Subscription.id == subscription_id))

    async def assign_node(self, host: VpsHost, node: ProxyNode) -> VpsNode:
        item = await self.session.scalar(
            select(VpsNode).where(
                VpsNode.host_id == host.id, VpsNode.fingerprint == node.fingerprint
            )
        )
        spec = ProxyNodeSpec(
            name=node.name,
            protocol=node.protocol,
            server=node.server,
            port=node.port,
            link=self.decrypt_node_link(node),
            tags=node.tags,
        )
        if item is None:
            item = VpsNode(
                host_id=host.id,
                source_node_id=node.id,
                name=spec.name,
                protocol=spec.protocol,
                server=spec.server,
                port=spec.port,
                encrypted_link=self.secret_box.encrypt(spec.link),
                fingerprint=node.fingerprint,
            )
            self.session.add(item)
        else:
            item.source_node_id = node.id
            self._copy_spec(item, spec)
        await self.session.flush()
        return item

    async def list_vps_nodes(self, host_id: int) -> Sequence[VpsNode]:
        stmt = (
            select(VpsNode)
            .where(VpsNode.host_id == host_id)
            .order_by(VpsNode.last_latency_ms.is_(None), VpsNode.last_latency_ms, VpsNode.name)
        )
        return (await self.session.scalars(stmt)).all()

    async def get_vps_node(self, item_id: int) -> VpsNode:
        item = await self.session.get(VpsNode, item_id)
        if item is None:
            raise KeyError("VPS node not found")
        return item

    async def assign_subscription(self, host: VpsHost, sub: Subscription) -> VpsSubscription:
        item = await self.session.scalar(
            select(VpsSubscription).where(
                VpsSubscription.host_id == host.id,
                VpsSubscription.source_subscription_id == sub.id,
            )
        )
        if item is None:
            item = VpsSubscription(
                host_id=host.id,
                source_subscription_id=sub.id,
                name=sub.name,
                encrypted_url=sub.encrypted_url,
                encrypted_content=sub.encrypted_content,
                node_count=sub.node_count,
                last_update_at=sub.last_update_at,
            )
            self.session.add(item)
        else:
            item.name = sub.name
            item.encrypted_url = sub.encrypted_url
            item.encrypted_content = sub.encrypted_content
            item.node_count = sub.node_count
            item.last_update_at = sub.last_update_at
        await self.session.flush()
        return item

    async def list_vps_subscriptions(self, host_id: int) -> Sequence[VpsSubscription]:
        stmt = (
            select(VpsSubscription)
            .where(VpsSubscription.host_id == host_id)
            .order_by(VpsSubscription.name)
        )
        return (await self.session.scalars(stmt)).all()

    async def get_vps_subscription(self, item_id: int) -> VpsSubscription:
        item = await self.session.get(VpsSubscription, item_id)
        if item is None:
            raise KeyError("VPS subscription not found")
        return item

    async def replace_vps_subscription_entries(
        self, sub: VpsSubscription, specs: Sequence[ProxyNodeSpec]
    ) -> list[VpsSubscriptionEntry]:
        current = {
            item.fingerprint: item
            for item in (
                await self.session.scalars(
                    select(VpsSubscriptionEntry).where(
                        VpsSubscriptionEntry.vps_subscription_id == sub.id
                    )
                )
            ).all()
        }
        seen: set[str] = set()
        saved: list[VpsSubscriptionEntry] = []
        for spec in specs:
            seen.add(spec.fingerprint)
            item = current.get(spec.fingerprint)
            if item is None:
                item = VpsSubscriptionEntry(
                    vps_subscription_id=sub.id,
                    name=spec.name,
                    protocol=spec.protocol,
                    server=spec.server,
                    port=spec.port,
                    fingerprint=spec.fingerprint,
                    encrypted_link=self.secret_box.encrypt(spec.link),
                )
                self.session.add(item)
            else:
                item.name = spec.name
                item.protocol = spec.protocol
                item.server = spec.server
                item.port = spec.port
                item.encrypted_link = self.secret_box.encrypt(spec.link)
            saved.append(item)
        stale = set(current) - seen
        if stale:
            await self.session.execute(
                delete(VpsSubscriptionEntry).where(
                    VpsSubscriptionEntry.vps_subscription_id == sub.id,
                    VpsSubscriptionEntry.fingerprint.in_(stale),
                )
            )
        sub.node_count = len(specs)
        sub.last_update_at = datetime.now(UTC)
        await self.session.flush()
        return saved

    async def list_vps_subscription_entries(
        self, vps_subscription_id: int, *, limit: int = 10, offset: int = 0
    ) -> Sequence[VpsSubscriptionEntry]:
        stmt = (
            select(VpsSubscriptionEntry)
            .where(VpsSubscriptionEntry.vps_subscription_id == vps_subscription_id)
            .order_by(
                VpsSubscriptionEntry.last_latency_ms.is_(None),
                VpsSubscriptionEntry.last_latency_ms,
                VpsSubscriptionEntry.name,
            )
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.scalars(stmt)).all()

    async def get_vps_subscription_entry(self, entry_id: int) -> VpsSubscriptionEntry:
        item = await self.session.get(VpsSubscriptionEntry, entry_id)
        if item is None:
            raise KeyError("VPS subscription entry not found")
        return item

    async def update_node_test(
        self,
        node: ProxyNode | SubscriptionEntry | VpsNode | VpsSubscriptionEntry,
        result: dict[str, Any],
    ) -> None:
        node.last_test = redact_obj(result)
        latency = result.get("access_latency_ms") or result.get("latency_ms")
        node.last_latency_ms = int(latency) if latency is not None else None
        node.status = NodeStatus.online if result.get("proxy_ok") else NodeStatus.offline

    async def create_task(
        self,
        *,
        kind: TaskKind,
        actor_user_id: int,
        host_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(
            kind=kind,
            actor_user_id=actor_user_id,
            host_id=host_id,
            payload=redact_obj(payload or {}),
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def recent_tasks(self, limit: int = 10) -> Sequence[Task]:
        return (
            await self.session.scalars(select(Task).order_by(desc(Task.created_at)).limit(limit))
        ).all()

    async def create_codex_task(self, candidate_id: int) -> CodexTask:
        task = CodexTask(candidate_id=candidate_id)
        self.session.add(task)
        await self.session.flush()
        return task

    async def get_codex_task(self, task_id: int) -> CodexTask:
        task = await self.session.get(CodexTask, task_id)
        if task is None:
            raise KeyError("Codex task not found")
        return task

    async def next_codex_task(self) -> CodexTask | None:
        stmt = (
            select(CodexTask)
            .where(CodexTask.status == CodexTaskStatus.queued)
            .order_by(CodexTask.id)
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def recent_codex_tasks(self, limit: int = 10) -> Sequence[CodexTask]:
        return (
            await self.session.scalars(
                select(CodexTask).order_by(desc(CodexTask.created_at)).limit(limit)
            )
        ).all()

    async def active_codex_task(self, candidate_id: int) -> CodexTask | None:
        return await self.session.scalar(
            select(CodexTask)
            .where(
                CodexTask.candidate_id == candidate_id,
                CodexTask.status.in_([CodexTaskStatus.queued, CodexTaskStatus.running]),
            )
            .order_by(desc(CodexTask.id))
            .limit(1)
        )

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
