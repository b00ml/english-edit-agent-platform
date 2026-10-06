"""Add normalized RAG documents and nullable chunk provenance/version fields.

No backfill or re-embedding: existing content/vector values remain untouched.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "rag_p0_abc"
down_revision = "p1_09_trace_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("blocks", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("stats", postgresql.JSONB(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_knowledge_document_tenant_id", "knowledge_document", ["tenant_id"])
    additions = {
        "document_id": sa.String(36),
        "chunk_index": sa.Integer(),
        "content_start": sa.Integer(),
        "content_end": sa.Integer(),
        "page_no": sa.Integer(),
        "context_header": sa.Text(),
        "section_path": postgresql.JSONB(),
        "parser_version": sa.String(64),
        "chunker_version": sa.String(64),
        "embedding_model": sa.String(128),
        "embedding_dimension": sa.Integer(),
        "embedding_content_hash": sa.String(64),
    }
    for name, type_ in additions.items():
        op.add_column("knowledge_chunk", sa.Column(name, type_, nullable=True))
    op.create_foreign_key(
        "fk_knowledge_chunk_document",
        "knowledge_chunk",
        "knowledge_document",
        ["document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_knowledge_chunk_document_id", "knowledge_chunk", ["document_id"])


def downgrade() -> None:
    # Existing chunk content, embedding and meta survive this downgrade.
    op.drop_index("ix_knowledge_chunk_document_id", table_name="knowledge_chunk")
    op.drop_constraint("fk_knowledge_chunk_document", "knowledge_chunk", type_="foreignkey")
    for name in (
        "document_id",
        "chunk_index",
        "content_start",
        "content_end",
        "page_no",
        "context_header",
        "section_path",
        "parser_version",
        "chunker_version",
        "embedding_model",
        "embedding_dimension",
        "embedding_content_hash",
    ):
        op.drop_column("knowledge_chunk", name)
    op.drop_index("ix_knowledge_document_tenant_id", table_name="knowledge_document")
    op.drop_table("knowledge_document")
