"""Persist per-draft quality evaluations, including rejected/failed paths."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "p0_04_quality_events"
down_revision = "m3_03_model_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_evaluation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("generation_task.id"), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("revise_count", sa.Integer, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("is_final", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("dimension_scores", postgresql.JSONB, nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("thread_id", "revise_count", name="uq_quality_eval_round"),
    )
    for name in ("task_id", "template_id", "thread_id", "tenant_id"):
        op.create_index(f"ix_quality_evaluation_{name}", "quality_evaluation", [name])


def downgrade() -> None:
    op.drop_table("quality_evaluation")
