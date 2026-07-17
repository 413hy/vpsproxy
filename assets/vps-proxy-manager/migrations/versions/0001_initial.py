"""initial schema"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proxy_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("encrypted_link", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column(
            "status", sa.Enum("unknown", "online", "offline", name="nodestatus"), nullable=False
        ),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_test", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fingerprint", name="uq_proxy_node_fingerprint"),
    )
    op.create_index("ix_proxy_nodes_name", "proxy_nodes", ["name"])
    op.create_index("ix_proxy_nodes_protocol", "proxy_nodes", ["protocol"])
    op.create_index("ix_proxy_nodes_server", "proxy_nodes", ["server"])
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("encrypted_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("update_interval_hours", sa.Integer(), nullable=True),
        sa.Column("last_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vps_hosts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column(
            "auth_method", sa.Enum("password", "private_key", name="authmethod"), nullable=False
        ),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("known_host", sa.Text(), nullable=True),
        sa.Column("system_info", sa.JSON(), nullable=False),
        sa.Column("current_node_id", sa.Integer(), sa.ForeignKey("proxy_nodes.id"), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("last_status", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_vps_hosts_host", "vps_hosts", ["host"])
    op.create_index("ix_vps_hosts_name", "vps_hosts", ["name"])
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "kind",
            sa.Enum(
                "detect",
                "status",
                "test_ssh",
                "import_subscription",
                "speedtest",
                "apply_proxy",
                "stop_proxy",
                "restore_proxy",
                "rollback",
                "uninstall",
                name="taskkind",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "cancel_requested",
                "succeeded",
                "failed",
                "rolled_back",
                "canceled",
                name="taskstatus",
            ),
            nullable=False,
        ),
        sa.Column("host_id", sa.Integer(), sa.ForeignKey("vps_hosts.id"), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tasks_kind", "tasks", ["kind"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_host_id", "tasks", ["host_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("host_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("result", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_host_id", "audit_logs", ["host_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("tasks")
    op.drop_table("vps_hosts")
    op.drop_table("subscriptions")
    op.drop_table("proxy_nodes")
