"""Reviewed OCR indexing state; no automatic paid calls or legacy data rewrite."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "rag_ocr_review"
down_revision = "rag_ocr_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        sa.Column("index_status", sa.String(32), nullable=False, server_default="not_requested"),
        sa.Column("approval", JSONB(), nullable=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_token", sa.String(36), nullable=True),
        sa.Column("index_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_dispatch_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_error", sa.String(512), nullable=True),
        sa.Column("indexed_document_id", sa.String(36), nullable=True),
    ]
    for column in columns:
        op.add_column("ocr_job", column)
    op.create_foreign_key(
        "fk_ocr_job_approved_by", "ocr_job", "users", ["approved_by"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_ocr_job_indexed_document",
        "ocr_job",
        "knowledge_document",
        ["indexed_document_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ocr_job_indexed_document", "ocr_job", type_="foreignkey")
    op.drop_constraint("fk_ocr_job_approved_by", "ocr_job", type_="foreignkey")
    for name in (
        "indexed_document_id",
        "index_error",
        "index_dispatch_after",
        "index_until",
        "index_token",
        "approved_at",
        "approved_by",
        "approval",
        "index_status",
    ):
        op.drop_column("ocr_job", name)
