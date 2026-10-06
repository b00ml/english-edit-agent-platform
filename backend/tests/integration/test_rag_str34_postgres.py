"""Migrated PG logical-table/coverage and durable review jobs; own tenant rollback only."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument, OcrBoundaryJob, OcrJob
from app.rag import retriever
from app.rag.indexer import index_document
from app.rag.structure import build_structure
from app.services.boundary_service import BoundaryService, WindowRequest
from app.services.knowledge_service import KnowledgeService, StructureReview
from tests.test_rag_str34 import reference_doc, source

pytestmark = pytest.mark.integration


@pytest.fixture
def actor(monkeypatch):
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "rules")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values],
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )
    return SimpleNamespace(
        id=None, role="researcher", tenant_id="str34-" + uuid.uuid4().hex, status="active"
    )


def test_pg_table_group_cell_confirmations_and_provenance(pg_session, actor):
    p = build_structure(source())
    cell = next(
        e for e in p["edges"] if e["relation"] == "table_cell_continues" and e["columns"] == [1, 2]
    )
    index_document(
        pg_session,
        "教材",
        "Fixture",
        source(),
        tenant_id=actor.tenant_id,
        structure_decisions={"accepted_edge_ids": [cell["id"]]},
    )
    details = []
    info = {}
    retriever.retrieve(
        pg_session,
        "first part",
        tenant_id=actor.tenant_id,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert not info["fallbacks"] and any(c["pages"] == [1, 2] for c in details)
    assert any(
        cell["id"] in v["confirmed_cell_joins"]
        for b in info["context_bundles"]
        for v in b["logical_table_views"]
    )


def test_pg_directed_references_and_two_question_coverage(pg_session, actor):
    index_document(pg_session, "教材", "Reference", reference_doc(), tenant_id=actor.tenant_id)
    details = []
    info = {}
    retriever.retrieve(
        pg_session,
        "General definition以及Exception applies",
        tenant_id=actor.tenant_id,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert not info["fallbacks"] and info["question_plan"]["parts"]
    assert "Exception applies" in " ".join(c["content"] for c in details)


def test_pg_boundary_request_idempotency_and_cancel(pg_session, actor):
    parent = OcrJob(
        tenant_id=actor.tenant_id,
        filename="fixture.pdf",
        source_hash="1" * 64,
        input_key="private/fixture.pdf",
        input_bytes=1,
        page_count=3,
        selected_pages=[1, 2, 3],
        status="completed",
        preview_hash="2" * 64,
        preview_key="preview.json",
    )
    pg_session.add(parent)
    pg_session.commit()
    svc = BoundaryService(pg_session, lambda _: None)
    request = WindowRequest(pages=[2, 3], local_compute_acknowledged=True)
    a = svc.create(parent.id, request, actor)
    b = svc.create(parent.id, request, actor)
    assert (
        a["id"] == b["id"]
        and pg_session.query(OcrBoundaryJob).filter_by(job_id=parent.id).count() == 1
    )
    assert svc.cancel(a["id"], actor)["status"] == "cancelled"
    assert svc.resume(a["id"], actor)["status"] == "pending"


def test_pg_delete_physical_continuation_never_injects_grid_from_snapshot(pg_session, actor):
    index_document(pg_session, "教材", "Fixture", source(), tenant_id=actor.tenant_id)
    doc = pg_session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == actor.tenant_id)
    )
    rows = pg_session.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)
    ).all()
    for row in rows:
        if row.page_no == 2:
            KnowledgeService(pg_session).delete_knowledge(row.id, actor)
    details = []
    retriever.retrieve(
        pg_session,
        "first part",
        tenant_id=actor.tenant_id,
        context_mode="relation",
        details=details,
    )
    assert "continuation" not in " ".join(c["content"] for c in details)
    assert pg_session.execute(text("SELECT 1")).scalar() == 1
