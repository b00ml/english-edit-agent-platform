"""Durable OCR jobs/pages; no changes to existing knowledge or generation rows."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "rag_ocr_jobs"
down_revision = "rag_p1_def"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("input_key", sa.String(256), nullable=False),
        sa.Column("input_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("selected_pages", JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatch_after", sa.DateTime(timezone=True)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("preview_key", sa.String(256)),
        sa.Column("preview_hash", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(512)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for name in ("tenant_id", "status", "lease_until"):
        op.create_index(f"ix_ocr_job_{name}", "ocr_job", [name])
    op.create_table(
        "ocr_page",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(36), sa.ForeignKey("ocr_job.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "attempt_history", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("result_key", sa.String(256)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("engine_version", sa.String(64)),
        sa.Column("latency_seconds", sa.Float()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(512)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("job_id", "page_no", name="uq_ocr_page_job_number"),
    )
    op.create_index("ix_ocr_page_job_id", "ocr_page", ["job_id"])


def downgrade() -> None:
    op.drop_table("ocr_page")
    op.drop_table("ocr_job")
