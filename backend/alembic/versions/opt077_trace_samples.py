"""Bounded encrypted per-call snapshots; old summaries remain intact."""

import sqlalchemy as sa

from alembic import op

revision = "opt077_trace_samples"
down_revision = "opt076_states_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trace_log",
        sa.Column(
            "snapshot_status", sa.String(32), nullable=False, server_default="legacy_unavailable"
        ),
    )
    op.create_table(
        "trace_snapshot",
        sa.Column(
            "trace_row_id",
            sa.String(36),
            sa.ForeignKey("trace_log.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("stored_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("replay_level", sa.String(48), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trace_snapshot_expires_at", "trace_snapshot", ["expires_at"])


def downgrade() -> None:
    op.drop_table("trace_snapshot")
    op.drop_column("trace_log", "snapshot_status")
