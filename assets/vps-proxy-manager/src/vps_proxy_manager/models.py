from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
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
    import_subscription = "import_subscription"
    speedtest = "speedtest"
    apply_proxy = "apply_proxy"
    stop_proxy = "stop_proxy"
    restore_proxy = "restore_proxy"
    rollback = "rollback"
    uninstall = "uninstall"


class NodeStatus(StrEnum):
    unknown = "unknown"
    online = "online"
    offline = "offline"


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
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    current_node: Mapped[ProxyNode | None] = relationship(foreign_keys=[current_node_id])


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    encrypted_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    update_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProxyNode(Base):
    __tablename__ = "proxy_nodes"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_proxy_node_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    protocol: Mapped[str] = mapped_column(String(32), index=True)
    server: Mapped[str] = mapped_column(String(253), index=True)
    port: Mapped[int] = mapped_column(Integer)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    encrypted_link: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.unknown)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind), index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.queued, index=True)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("vps_hosts.id"), nullable=True, index=True)
    actor_user_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
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
