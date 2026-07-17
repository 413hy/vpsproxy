"""separate controller and VPS proxy domains

Revision ID: 0002_separate_proxy_domains
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_separate_proxy_domains"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


node_status = sa.Enum("unknown", "online", "offline", name="nodestatus")
host_lifecycle = sa.Enum(
    "pending", "provisioning", "ready", "failed", "disabled", name="hostlifecycle"
)
proxy_mode = sa.Enum("unknown", "local", "proxy", "uninstalled", name="proxymode")
resource_kind = sa.Enum("node", "subscription", name="resourcekind")
codex_status = sa.Enum(
    "queued", "running", "succeeded", "failed", "canceled", name="codextaskstatus"
)


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("encrypted_content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("content_format", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("node_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_test", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.create_index("ix_subscriptions_name", ["name"])

    with op.batch_alter_table("vps_hosts") as batch:
        batch.add_column(
            sa.Column("lifecycle", host_lifecycle, nullable=False, server_default="ready")
        )
        batch.add_column(sa.Column("remote_agent_version", sa.String(length=40), nullable=True))

    op.create_table(
        "subscription_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("encrypted_link", sa.Text(), nullable=False),
        sa.Column("status", node_status, nullable=False),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_test", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subscription_id", "fingerprint", name="uq_subscription_entry"),
    )
    op.create_index(
        "ix_subscription_entries_subscription_id", "subscription_entries", ["subscription_id"]
    )
    op.create_index("ix_subscription_entries_name", "subscription_entries", ["name"])
    op.create_index("ix_subscription_entries_protocol", "subscription_entries", ["protocol"])
    op.create_index("ix_subscription_entries_fingerprint", "subscription_entries", ["fingerprint"])

    # In 0.1, parsed subscription nodes lived in proxy_nodes. Preserve them as
    # subscription-owned cache rows while keeping the new single-node library filtered.
    op.execute(
        sa.text(
            """
            INSERT INTO subscription_entries (
                subscription_id, name, protocol, server, port, fingerprint,
                encrypted_link, status, last_latency_ms, last_test, updated_at
            )
            SELECT
                subscription_id, name, protocol, server, port, fingerprint,
                encrypted_link, status, last_latency_ms, last_test, updated_at
            FROM proxy_nodes
            WHERE subscription_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE subscriptions
            SET node_count = (
                SELECT COUNT(*) FROM subscription_entries
                WHERE subscription_entries.subscription_id = subscriptions.id
            )
            """
        )
    )

    op.create_table(
        "vps_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column(
            "auth_method", sa.Enum("password", "private_key", name="authmethod"), nullable=False
        ),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("known_host", sa.Text(), nullable=False),
        sa.Column("system_info", sa.JSON(), nullable=False),
        sa.Column("lifecycle", host_lifecycle, nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vps_candidates_name", "vps_candidates", ["name"])
    op.create_index("ix_vps_candidates_host", "vps_candidates", ["host"])

    op.create_table(
        "vps_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "host_id",
            sa.Integer(),
            sa.ForeignKey("vps_hosts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_node_id",
            sa.Integer(),
            sa.ForeignKey("proxy_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("encrypted_link", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", node_status, nullable=False),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_test", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("host_id", "fingerprint", name="uq_vps_node"),
    )
    op.create_index("ix_vps_nodes_host_id", "vps_nodes", ["host_id"])
    op.create_index("ix_vps_nodes_source_node_id", "vps_nodes", ["source_node_id"])
    op.create_index("ix_vps_nodes_name", "vps_nodes", ["name"])
    op.create_index("ix_vps_nodes_fingerprint", "vps_nodes", ["fingerprint"])

    op.create_table(
        "vps_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "host_id",
            sa.Integer(),
            sa.ForeignKey("vps_hosts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("encrypted_url", sa.Text(), nullable=False),
        sa.Column("encrypted_content", sa.Text(), nullable=True),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("last_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("host_id", "source_subscription_id", name="uq_vps_subscription_source"),
    )
    op.create_index("ix_vps_subscriptions_host_id", "vps_subscriptions", ["host_id"])
    op.create_index(
        "ix_vps_subscriptions_source_subscription_id",
        "vps_subscriptions",
        ["source_subscription_id"],
    )
    op.create_index("ix_vps_subscriptions_name", "vps_subscriptions", ["name"])

    op.create_table(
        "vps_subscription_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "vps_subscription_id",
            sa.Integer(),
            sa.ForeignKey("vps_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("encrypted_link", sa.Text(), nullable=False),
        sa.Column("status", node_status, nullable=False),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_test", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("vps_subscription_id", "fingerprint", name="uq_vps_subscription_entry"),
    )
    op.create_index(
        "ix_vps_subscription_entries_vps_subscription_id",
        "vps_subscription_entries",
        ["vps_subscription_id"],
    )
    op.create_index("ix_vps_subscription_entries_name", "vps_subscription_entries", ["name"])
    op.create_index(
        "ix_vps_subscription_entries_fingerprint", "vps_subscription_entries", ["fingerprint"]
    )

    op.create_table(
        "vps_proxy_states",
        sa.Column(
            "host_id",
            sa.Integer(),
            sa.ForeignKey("vps_hosts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("mode", proxy_mode, nullable=False),
        sa.Column("current_kind", resource_kind, nullable=True),
        sa.Column(
            "current_vps_node_id",
            sa.Integer(),
            sa.ForeignKey("vps_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "current_vps_subscription_id",
            sa.Integer(),
            sa.ForeignKey("vps_subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("current_entry_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("current_display_name", sa.String(length=160), nullable=True),
        sa.Column("last_switch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "codex_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("vps_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("status", codex_status, nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_codex_tasks_candidate_id", "codex_tasks", ["candidate_id"])
    op.create_index("ix_codex_tasks_status", "codex_tasks", ["status"])


def downgrade() -> None:
    op.drop_table("codex_tasks")
    op.drop_table("vps_proxy_states")
    op.drop_table("vps_subscription_entries")
    op.drop_table("vps_subscriptions")
    op.drop_table("vps_nodes")
    op.drop_table("vps_candidates")
    op.drop_table("subscription_entries")
    with op.batch_alter_table("vps_hosts") as batch:
        batch.drop_column("remote_agent_version")
        batch.drop_column("lifecycle")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_index("ix_subscriptions_name")
        batch.drop_column("updated_at")
        batch.drop_column("last_test")
        batch.drop_column("node_count")
        batch.drop_column("content_format")
        batch.drop_column("encrypted_content")
