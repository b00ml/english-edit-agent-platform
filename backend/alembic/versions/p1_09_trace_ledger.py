"""Record usage provenance and per-side immutable cost estimates."""

import sqlalchemy as sa

from alembic import op

revision = "p1_09_trace_ledger"
down_revision = "p1_03_rag_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trace_log", sa.Column("usage_reported", sa.Boolean, nullable=True))
    op.add_column("trace_log", sa.Column("prompt_cost", sa.Float, nullable=True))
    op.add_column("trace_log", sa.Column("completion_cost", sa.Float, nullable=True))


def downgrade() -> None:
    for column in ("completion_cost", "prompt_cost", "usage_reported"):
        op.drop_column("trace_log", column)
