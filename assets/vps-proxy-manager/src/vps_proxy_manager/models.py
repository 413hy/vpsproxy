from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AuthMethod(StrEnum):
    password = "password"  # noqa: S105
    private_key = "private_key"


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    cancel_requested = "cancel_requested"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"
    canceled = "canceled"


class TaskKind(StrEnum):
    detect = "detect"
    status = "status"
    test_ssh = "test_ssh"
    import_subscription = "import_subscription"  # Legacy 0.1 task, retained for DB reads.
    speedtest = "speedtest"  # Legacy 0.1 task, retained for DB reads.
    local_node_test = "local_node_test"
    local_subscription_test = "local_subscription_test"
    sync_node = "sync_node"
    sync_subscription = "sync_subscription"
    vps_node_test = "vps_node_test"
    vps_subscription_test = "vps_subscription_test"
    apply_proxy = "apply_proxy"
    stop_proxy = "stop_proxy"
    restore_proxy = "restore_proxy"
    rollback = "rollback"
    remove_vps_node = "remove_vps_node"
    remove_vps_subscription = "remove_vps_subscription"
    delete_source_node = "delete_source_node"
    delete_source_subscription = "delete_source_subscription"
    uninstall = "uninstall"
    delete_host = "delete_host"
    consistency_check = "consistency_check"


class NodeStatus(StrEnum):
    unknown = "unknown"
    online = "online"
    offline = "offline"


class HostLifecycle(StrEnum):
    pending = "pending"
    provisioning = "provisioning"
    ready = "ready"
    failed = "failed"
    disabled = "disabled"


class ProxyMode(StrEnum):
    unknown = "unknown"
    local = "local"
    proxy = "proxy"
    uninstalled = "uninstalled"


class ResourceKind(StrEnum):
    node = "node"
    subscription = "subscription"


class CodexTaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    encrypted_url: Mapped[str] = mapped_column(Text)
    encrypted_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_format: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    update_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProxyNode(Base):
    """Controller-owned single-node library.

    subscription_id is retained only to keep old installations readable. New
    subscription entries live in subscription_entries and never enter this table.
    """

    __tablename__ = "proxy_nodes"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_proxy_node_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    protocol: Mapped[str] = mapped_column(String(32), index=True)
    server: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True
    )
    encrypted_link: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.unknown)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SubscriptionEntry(Base):
    __tablename__ = "subscription_entries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "fingerprint", name="uq_subscription_entry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), index=True)
    protocol: Mapped[str] = mapped_column(String(32), index=True)
    server: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_link: Mapped[str] = mapped_column(Text)
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.unknown)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    subscription: Mapped[Subscription] = relationship()


class VpsCandidate(Base):
    __tablename__ = "vps_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[AuthMethod] = mapped_column(Enum(AuthMethod))
    encrypted_secret: Mapped[str] = mapped_column(Text)
    known_host: Mapped[str] = mapped_column(Text)
    system_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lifecycle: Mapped[HostLifecycle] = mapped_column(
        Enum(HostLifecycle), default=HostLifecycle.pending
    )
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="等待 Codex 初始化")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class VpsHost(Base):
    __tablename__ = "vps_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[AuthMethod] = mapped_column(Enum(AuthMethod))
    encrypted_secret: Mapped[str] = mapped_column(Text)
    known_host: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_node_id: Mapped[int | None] = mapped_column(ForeignKey("proxy_nodes.id"), nullable=True)
    lifecycle: Mapped[HostLifecycle] = mapped_column(
        Enum(HostLifecycle), default=HostLifecycle.ready
    )
    remote_agent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    current_node: Mapped[ProxyNode | None] = relationship(foreign_keys=[current_node_id])


class VpsNode(Base):
    __tablename__ = "vps_nodes"
    __table_args__ = (UniqueConstraint("host_id", "fingerprint", name="uq_vps_node"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("vps_hosts.id", ondelete="CASCADE"), index=True)
    source_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxy_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160), index=True)
    protocol: Mapped[str] = mapped_column(String(32))
    server: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer)
    encrypted_link: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.unknown)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    host: Mapped[VpsHost] = relationship()
    source_node: Mapped[ProxyNode | None] = relationship()


class VpsSubscription(Base):
    __tablename__ = "vps_subscriptions"
    __table_args__ = (
        UniqueConstraint("host_id", "source_subscription_id", name="uq_vps_subscription_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("vps_hosts.id", ondelete="CASCADE"), index=True)
    source_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100), index=True)
    encrypted_url: Mapped[str] = mapped_column(Text)
    encrypted_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    host: Mapped[VpsHost] = relationship()
    source_subscription: Mapped[Subscription | None] = relationship()


class VpsSubscriptionEntry(Base):
    __tablename__ = "vps_subscription_entries"
    __table_args__ = (
        UniqueConstraint("vps_subscription_id", "fingerprint", name="uq_vps_subscription_entry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vps_subscription_id: Mapped[int] = mapped_column(
        ForeignKey("vps_subscriptions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), index=True)
    protocol: Mapped[str] = mapped_column(String(32))
    server: Mapped[str] = mapped_column(String(253))
    port: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_link: Mapped[str] = mapped_column(Text)
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.unknown)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    vps_subscription: Mapped[VpsSubscription] = relationship()


class VpsProxyState(Base):
    __tablename__ = "vps_proxy_states"

    host_id: Mapped[int] = mapped_column(
        ForeignKey("vps_hosts.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[ProxyMode] = mapped_column(Enum(ProxyMode), default=ProxyMode.local)
    current_kind: Mapped[ResourceKind | None] = mapped_column(Enum(ResourceKind), nullable=True)
    current_vps_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("vps_nodes.id", ondelete="SET NULL"), nullable=True
    )
    current_vps_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("vps_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    current_entry_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind), index=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.queued, index=True
    )
    host_id: Mapped[int | None] = mapped_column(
        ForeignKey("vps_hosts.id"), nullable=True, index=True
    )
    actor_user_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CodexTask(Base):
    __tablename__ = "codex_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("vps_candidates.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, unique=True, index=True
    )
    operation: Mapped[str] = mapped_column(String(40), default="provision")
    status: Mapped[CodexTaskStatus] = mapped_column(
        Enum(CodexTaskStatus), default=CodexTaskStatus.queued, index=True
    )
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="等待 Codex Worker")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(Integer, index=True)
    host_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    result: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
