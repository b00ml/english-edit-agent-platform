"""RAG P1: parent/child lineage, tags, hybrid search and ANN indexes.

Existing vectors are retained. Legacy keyword text is backfilled without embedding.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "rag_p1_def"
down_revision = "rag_p0_abc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, type_ in {
        "parent_chunk_id": sa.String(36),
        "prev_chunk_id": sa.String(36),
        "next_chunk_id": sa.String(36),
        "chunk_type": sa.String(32),
        "content_hash": sa.String(64),
        "search_text": sa.Text(),
        "knowledge_point_ids": postgresql.JSONB(),
        "knowledge_point_labels": postgresql.JSONB(),
    }.items():
        op.add_column("knowledge_chunk", sa.Column(name, type_, nullable=True))
    op.alter_column("knowledge_chunk", "embedding", nullable=True)
    for field, deletion in [
        ("parent_chunk_id", "CASCADE"),
        ("prev_chunk_id", "SET NULL"),
        ("next_chunk_id", "SET NULL"),
    ]:
        op.create_foreign_key(
            f"fk_knowledge_chunk_{field}",
            "knowledge_chunk",
            "knowledge_chunk",
            [field],
            ["id"],
            ondelete=deletion,
        )
    op.create_index("ix_knowledge_chunk_parent_chunk_id", "knowledge_chunk", ["parent_chunk_id"])
    op.create_index("ix_knowledge_chunk_chunk_type", "knowledge_chunk", ["chunk_type"])
    op.execute("""UPDATE knowledge_chunk SET search_text = lower(
        coalesce(source_name,'') || ' ' || coalesce(knowledge_point,'') || ' ' ||
        coalesce(context_header,'') || ' ' || coalesce(content,''))""")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("""CREATE INDEX ix_knowledge_chunk_fts ON knowledge_chunk USING gin
        (to_tsvector('english'::regconfig, coalesce(search_text,'')))""")
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_trgm ON knowledge_chunk "
        "USING gin (search_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_point_ids ON knowledge_chunk "
        "USING gin (knowledge_point_ids)"
    )
    op.execute("""CREATE INDEX ix_knowledge_chunk_hnsw ON knowledge_chunk USING hnsw
        (embedding vector_cosine_ops) WHERE embedding IS NOT NULL AND
        (chunk_type IS NULL OR chunk_type != 'parent')""")


def downgrade() -> None:
    for name in [
        "ix_knowledge_chunk_hnsw",
        "ix_knowledge_chunk_point_ids",
        "ix_knowledge_chunk_trgm",
        "ix_knowledge_chunk_fts",
    ]:
        op.drop_index(name, table_name="knowledge_chunk")
    # Delete only P1 context-only parents; retain all leaf content/vector rows.
    op.execute(
        "UPDATE knowledge_chunk SET parent_chunk_id=NULL, prev_chunk_id=NULL, next_chunk_id=NULL"
    )
    op.execute("DELETE FROM knowledge_chunk WHERE chunk_type='parent' AND embedding IS NULL")
    # Refuse a lossy rollback if an unexpected vector-less legacy row exists.
    op.alter_column("knowledge_chunk", "embedding", nullable=False)
    for field in ["parent_chunk_id", "prev_chunk_id", "next_chunk_id"]:
        op.drop_constraint(f"fk_knowledge_chunk_{field}", "knowledge_chunk", type_="foreignkey")
    op.drop_index("ix_knowledge_chunk_parent_chunk_id", table_name="knowledge_chunk")
    op.drop_index("ix_knowledge_chunk_chunk_type", table_name="knowledge_chunk")
    for name in [
        "parent_chunk_id",
        "prev_chunk_id",
        "next_chunk_id",
        "chunk_type",
        "content_hash",
        "search_text",
        "knowledge_point_ids",
        "knowledge_point_labels",
    ]:
        op.drop_column("knowledge_chunk", name)
    # Shared extensions are deliberately not dropped.
