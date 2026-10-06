"""Explicit multi-page OCR review jobs; no rewrite of knowledge or successful page checkpoints."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "rag_str34_boundary"
down_revision = "rag_ocr_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ocr_boundary_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("ocr_job.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("pages", JSONB(), nullable=False),
        sa.Column("source_preview_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_key", sa.String(256)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("engine_version", sa.String(64)),
        sa.Column("error", sa.String(256)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for column in ["job_id", "tenant_id", "status"]:
        op.create_index("ix_ocr_boundary_job_" + column, "ocr_boundary_job", [column])


def downgrade():
    op.drop_table("ocr_boundary_job")
