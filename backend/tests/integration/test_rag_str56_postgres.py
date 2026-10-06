"""Real PG segmented structural indexing, atomic selective replacement, no paid models."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.errors import InvalidKnowledgeError
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.indexer import index_document
from app.services.knowledge_service import KnowledgeService
from tests.test_rag_str12 import source

pytestmark = pytest.mark.integration


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setattr(settings, "RAG_PARENT_MIN_CHARS", 1)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values],
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )
    return SimpleNamespace(
        id=None, role="researcher", status="active", tenant_id="str56-" + uuid.uuid4().hex
    )


def test_pg_structural_composite_leaf_returns_exact_pages_and_offsets(pg_session, actor):
    index_document(
        pg_session, "教材", "Fixture", source(), tenant_id=actor.tenant_id, chunk_layout="structure"
    )
    doc = pg_session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == actor.tenant_id)
    )
    leafs = pg_session.scalars(
        select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == doc.id, KnowledgeChunk.chunk_type != "parent"
        )
    ).all()
    assert all(
        c.content_start is None and c.chunker_version == "rag-chunker-structure-v1" for c in leafs
    )
    citations = []
    info = {}
    retriever.retrieve(
        pg_session,
        "or表示",
        tenant_id=actor.tenant_id,
        context_mode="relation",
        details=citations,
        diagnostics=info,
    )
    assert not info["fallbacks"] and any(c["pages"] == [2, 3] for c in citations)
    assert all(
        s["content"] == doc.normalized_text[s["content_start"] : s["content_end"]]
        for c in citations
        for s in c["source_segments"]
    )


def test_pg_failed_structural_rebuild_keeps_old_rows_then_success_same_id(
    pg_session, actor, monkeypatch
):
    index_document(
        pg_session,
        "教材",
        "Fixture",
        source(),
        tenant_id=actor.tenant_id,
        meta={"index_revision": 1},
        chunk_layout="legacy",
    )
    doc = pg_session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == actor.tenant_id)
    )
    ids = set(
        pg_session.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id))
    )
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[float("nan")] * 1024 for _ in values],
    )
    with pytest.raises(InvalidKnowledgeError):
        index_document(
            pg_session,
            "教材",
            "Fixture",
            source(),
            tenant_id=actor.tenant_id,
            document_id=doc.id,
            replace_existing=True,
            chunk_layout="structure",
            meta={"index_revision": 2},
        )
    assert (
        set(
            pg_session.scalars(
                select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id)
            )
        )
        == ids
    )
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values],
    )
    index_document(
        pg_session,
        "教材",
        "Fixture",
        source(),
        tenant_id=actor.tenant_id,
        document_id=doc.id,
        replace_existing=True,
        chunk_layout="structure",
        meta={"index_revision": 2},
    )
    pg_session.refresh(doc)
    assert doc.meta["index_revision"] == 2 and doc.meta["chunk_layout"] == "structure"
    assert not ids.intersection(
        pg_session.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id))
    )


def test_pg_deleted_structural_leaf_cannot_be_recovered_from_document_snapshot(pg_session, actor):
    index_document(
        pg_session, "教材", "Fixture", source(), tenant_id=actor.tenant_id, chunk_layout="structure"
    )
    doc = pg_session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == actor.tenant_id)
    )
    row = next(
        c
        for c in pg_session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)
        )
        if "Hurry up" in c.content
    )
    KnowledgeService(pg_session).delete_knowledge(row.id, actor)
    citations = []
    retriever.retrieve(
        pg_session, "or", tenant_id=actor.tenant_id, context_mode="relation", details=citations
    )
    assert "Hurry up" not in " ".join(c["content"] for c in citations)
    assert "Hurry up" in doc.normalized_text and doc.status == "partial_index"
