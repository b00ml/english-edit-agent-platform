"""Migrated PG STR-1/2 and metadata-only review; UUID tenant transaction rollback only."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.indexer import index_document
from app.services.knowledge_service import KnowledgeService, StructureReview
from tests.test_rag_str12 import source

pytestmark = pytest.mark.integration


@pytest.fixture
def indexed(pg_session, monkeypatch):
    # STR1/2 metadata-only review is tested against unchanged legacy leaf boundaries.
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "legacy")
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "legacy")
    monkeypatch.setattr(
        retriever, "embed_texts", lambda inputs, **kwargs: [[1.0] + [0.0] * 1023 for _ in inputs]
    )
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda inputs, **kwargs: [[1.0] + [0.0] * 1023 for _ in inputs],
    )
    who = SimpleNamespace(id=None, tenant_id="str-test-" + uuid.uuid4().hex, role="researcher")
    index_document(pg_session, "教材", "Fixture", source(), tenant_id=who.tenant_id)
    doc = pg_session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == who.tenant_id)
    )
    return pg_session, who, doc


def test_pg_relation_returns_both_pages_and_actual_source_hashes(indexed):
    db, who, doc = indexed
    details = []
    info = {}
    retriever.retrieve(
        db,
        "or",
        tenant_id=who.tenant_id,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert any(c["pages"] == [2, 3] for c in details)
    assert all(
        segment["document_id"] == doc.id for c in details for segment in c["source_segments"]
    )
    assert not info["fallbacks"] and info["protocol_version"] == "rag-context-v2"
    assert db.execute(text("SELECT 1")).scalar() == 1


def test_pg_free_structure_review_preserves_leafs_and_binds_revision(indexed, monkeypatch):
    db, who, doc = indexed
    service = KnowledgeService(db)
    plan = service.structure_preview(doc.id, who)
    ids = set(db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id)))
    edge = next(e for e in plan["edges"] if e["from"] == "block:3" and e["to"] == "block:5")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda *a, **k: pytest.fail("free review cannot pay")
    )
    result = service.review_structure(
        doc.id,
        StructureReview(
            review_signature=plan["review_signature"],
            expected_index_revision=1,
            source_reviewed=True,
            rejected_edge_ids=[edge["id"]],
        ),
        who,
    )
    assert result["embedding_calls"] == 0 and result["structure_revision"] == 1
    assert (
        set(db.scalars(select(KnowledgeChunk.id).where(KnowledgeChunk.document_id == doc.id)))
        == ids
    )
    details = []
    retriever.retrieve(db, "or", tenant_id=who.tenant_id, context_mode="relation", details=details)
    assert all(not (c["pages"] == [2, 3]) for c in details)


def test_pg_delete_blocks_expansion_even_though_document_keeps_original_text(indexed):
    db, who, doc = indexed
    rows = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)).all()
    continuation = next(row for row in rows if "Hurry up" in row.content)
    KnowledgeService(db).delete_knowledge(continuation.id, who)
    assert "Hurry up" in db.get(KnowledgeDocument, doc.id).normalized_text
    details = []
    retriever.retrieve(db, "or", tenant_id=who.tenant_id, context_mode="relation", details=details)
    assert "Hurry up" not in " ".join(c["content"] for c in details)


def test_pg_explicit_legacy_context_is_still_available(indexed):
    db, who, _ = indexed
    details = []
    info = {}
    retriever.retrieve(
        db, "or", tenant_id=who.tenant_id, context_mode="legacy", details=details, diagnostics=info
    )
    assert details and info["protocol_version"] == "rag-context-v1" and not info["context_bundles"]
