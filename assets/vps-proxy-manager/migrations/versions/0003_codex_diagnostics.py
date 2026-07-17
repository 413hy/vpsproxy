"""add automatic Codex diagnostics

Revision ID: 0003_codex_diagnostics
Revises: 0002_separate_proxy_domains
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_codex_diagnostics"
down_revision = "0002_separate_proxy_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("codex_tasks") as batch:
        batch.alter_column("candidate_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("source_task_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_codex_tasks_source_task_id_tasks",
            "tasks",
            ["source_task_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_codex_tasks_source_task_id", ["source_task_id"])
        batch.create_index("ix_codex_tasks_source_task_id", ["source_task_id"])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM codex_tasks WHERE candidate_id IS NULL"))
    with op.batch_alter_table("codex_tasks") as batch:
        batch.drop_index("ix_codex_tasks_source_task_id")
        batch.drop_constraint("uq_codex_tasks_source_task_id", type_="unique")
        batch.drop_constraint("fk_codex_tasks_source_task_id_tasks", type_="foreignkey")
        batch.drop_column("source_task_id")
        batch.alter_column("candidate_id", existing_type=sa.Integer(), nullable=False)
