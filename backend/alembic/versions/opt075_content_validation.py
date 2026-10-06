"""Persist deterministic content validation separately from RAG provenance."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "opt075_content_validation"
down_revision = "opt074_delivery_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_item", sa.Column("validation_report", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("content_item", "validation_report")
