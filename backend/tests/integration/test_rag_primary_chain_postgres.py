"""Primary default layout on migrated PG; explicit legacy and atomic selective rollback."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import InvalidKnowledgeError
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.indexer import index_document
from tests.test_rag_str12 import source

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "structure")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda texts, **kw: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda texts, **kw: [[1.0] + [0.0] * 1023 for _ in texts]
    )


def test_pg_default_index_and_retrieval_use_structural_segments(pg_session: Session) -> None:
    tenant = "primary-" + uuid.uuid4().hex
    index_document(pg_session, "教材", "Primary fixture", source(), tenant_id=tenant)
    doc = pg_session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant))
    rows = list(
        pg_session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    )
    leafs = [r for r in rows if r.chunk_type != "parent"]
    assert doc.meta["chunk_layout"] == "structure"
    assert leafs and all(r.content_start is None and r.content_end is None for r in leafs)
    assert any(len(r.meta["chunk"]["pages"]) > 1 for r in leafs)
    details, info = [], {}
    texts = retriever.retrieve(
        pg_session, "or", tenant_id=tenant, details=details, diagnostics=info
    )
    assert (
        texts
        and info["context_mode"] == "relation"
        and info["protocol_version"] == "rag-context-v2"
    )
    assert all(s["document_id"] == doc.id for c in details for s in c["source_segments"])


def test_pg_explicit_legacy_layout_still_indexes_contiguous_leafs(pg_session: Session) -> None:
    tenant = "legacy-" + uuid.uuid4().hex
    index_document(
        pg_session, "教材", "Legacy fixture", source(), tenant_id=tenant, chunk_layout="legacy"
    )
    doc = pg_session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant))
    leafs = list(
        pg_session.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.document_id == doc.id, KnowledgeChunk.chunk_type != "parent"
            )
        )
    )
    assert doc.meta["chunk_layout"] == "legacy"
    assert all(r.content_start is not None and r.content_end is not None for r in leafs)


def test_pg_default_selective_rebuild_keeps_old_index_if_vectors_invalid(
    pg_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "rollback-" + uuid.uuid4().hex
    index_document(
        pg_session,
        "教材",
        "Fixture",
        source(),
        tenant_id=tenant,
        chunk_layout="legacy",
        meta={"index_revision": 1},
    )
    doc = pg_session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant))
    old_ids = set(
        pg_session.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id))
    )
    old_source = doc.content_hash, doc.source_hash, doc.normalized_text
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda values, **kw: [[float("nan")] * 1024 for _ in values]
    )
    with pytest.raises(InvalidKnowledgeError):
        index_document(
            pg_session,
            "教材",
            "Fixture",
            source(),
            tenant_id=tenant,
            document_id=doc.id,
            replace_existing=True,
            meta={"index_revision": 2},
        )
    assert (
        set(
            pg_session.scalars(
                select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id)
            )
        )
        == old_ids
    )
    assert doc.meta["index_revision"] == 1 and doc.meta["chunk_layout"] == "legacy"
    assert (doc.content_hash, doc.source_hash, doc.normalized_text) == old_source
