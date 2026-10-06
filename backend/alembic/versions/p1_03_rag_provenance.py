"""Store explicit RAG provenance independently of question output schema."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "p1_03_rag_provenance"
down_revision = "p1_01_tenant_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_item", sa.Column("provenance", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("content_item", "provenance")
