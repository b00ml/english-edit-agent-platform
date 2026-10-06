"""Durable item dispatch, fenced Outbox claims and retry deadlines; no historical replay."""

import sqlalchemy as sa

from alembic import op

revision = "opt074_delivery_leases"
down_revision = "rag_str34_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_outbox", sa.Column("retry_base", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("task_outbox", sa.Column("lease_token", sa.String(36), nullable=True))
    op.add_column(
        "task_outbox", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "generation_task_item", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("generation_task_item", "retry_after")
    for name in ["lease_until", "lease_token", "retry_base"]:
        op.drop_column("task_outbox", name)
