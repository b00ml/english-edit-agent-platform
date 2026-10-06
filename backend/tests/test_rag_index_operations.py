"""Index withdrawal/rebuild invariants. Fake vectors only in isolated unit database."""

from __future__ import annotations

import pytest

from app.config import settings
from app.errors import InvalidKnowledgeError, TenantScopeDeniedError
from app.models import KnowledgeChunk, KnowledgeDocument, OcrJob
from app.rag.knowledge_points import KnowledgeScope
from app.rag.retrieval.context import expand
from app.rag.retrieval.models import Candidate
from app.services.knowledge_service import KnowledgeService
from app.services.ocr_review import approve
from app.worker.ocr_index import run_index
from tests.test_ocr_review import confirmation, ready
from tests.test_ocr_review import runtime as review_operations_runtime
from tests.test_ocr_review import vectors


@pytest.fixture
def operations_runtime(db, tmp_path, monkeypatch):
    return review_operations_runtime.__wrapped__(db, tmp_path, monkeypatch)


def indexed(db, operations_runtime, monkeypatch):
    identifier = ready(db, operations_runtime)
    calls = []
    vectors(monkeypatch, calls)
    approve(
        db, identifier, confirmation(db, identifier, operations_runtime[1]), operations_runtime[1]
    )
    assert run_index(identifier, operations_runtime[2])["status"] == "indexed"
    return identifier, calls


def test_rebuild_is_explicit_and_successful_swap_increments_revision(
    db, operations_runtime, monkeypatch
):
    identifier, calls = indexed(db, operations_runtime, monkeypatch)
    document = db.query(KnowledgeDocument).one()
    old_ids = {row.id for row in db.query(KnowledgeChunk)}
    body = confirmation(
        db, identifier, operations_runtime[1], rebuild_index=True, expected_index_revision=1
    )
    approve(db, identifier, body, operations_runtime[1])
    assert db.query(KnowledgeDocument).one().id == document.id
    assert {row.id for row in db.query(KnowledgeChunk)} == old_ids  # live before replacement
    assert run_index(identifier, operations_runtime[2])["status"] == "indexed"
    db.expire_all()
    document = db.query(KnowledgeDocument).one()
    assert document.meta["index_revision"] == 2 and document.status == "indexed"
    assert not old_ids & {row.id for row in db.query(KnowledgeChunk)}
    assert len(calls) >= 2
    assert (
        operations_runtime[0].summary(db.get(OcrJob, identifier))["review_settings"][
            "excluded_block_ids"
        ]
        == []
    )
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, body, operations_runtime[1])


def test_failed_rebuild_keeps_all_old_vectors_content_and_revision(
    db, operations_runtime, monkeypatch
):
    identifier, calls = indexed(db, operations_runtime, monkeypatch)
    document = db.query(KnowledgeDocument).one()
    old_ids = {row.id for row in db.query(KnowledgeChunk)}
    old_hash = document.content_hash
    body = confirmation(
        db, identifier, operations_runtime[1], rebuild_index=True, expected_index_revision=1
    )
    approve(db, identifier, body, operations_runtime[1])
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda texts, **kwargs: [[float("nan")] * settings.EMBEDDING_DIM for _ in texts],
    )
    assert run_index(identifier, operations_runtime[2])["status"] == "needs_attention"
    db.expire_all()
    document = db.query(KnowledgeDocument).one()
    assert document.meta["index_revision"] == 1 and document.content_hash == old_hash
    assert {row.id for row in db.query(KnowledgeChunk)} == old_ids


def test_index_withdrawal_retains_ocr_and_can_be_reviewed_again(
    db, operations_runtime, monkeypatch
):
    identifier, calls = indexed(db, operations_runtime, monkeypatch)
    document = db.query(KnowledgeDocument).one()
    parent = KnowledgeChunk(
        document_id=document.id,
        tenant_id=document.tenant_id,
        source_type="教材",
        source_name="fixture",
        content="parent",
        chunk_type="parent",
    )
    db.add(parent)
    db.flush()
    child = db.query(KnowledgeChunk).filter(KnowledgeChunk.chunk_type != "parent").first()
    child.parent_chunk_id = parent.id
    child.chunk_type = "child"
    db.commit()
    expected_count = db.query(KnowledgeChunk).count()
    result = KnowledgeService(db).delete_document(document.id, operations_runtime[1])
    assert result["deleted_chunks"] == expected_count
    assert result["ocr_source_retained"] and db.query(KnowledgeChunk).count() == 0
    assert db.query(KnowledgeDocument).count() == 0
    job = db.get(OcrJob, identifier)
    db.refresh(job)
    assert job.index_status == "removed" and job.preview_key and job.status == "completed"
    approve(
        db, identifier, confirmation(db, identifier, operations_runtime[1]), operations_runtime[1]
    )
    assert run_index(identifier, operations_runtime[2])["status"] == "indexed"
    assert db.query(KnowledgeDocument).count() == 1


def test_deleted_child_is_not_reinjected_by_surviving_parent(db, operations_runtime, monkeypatch):
    identifier, calls = indexed(db, operations_runtime, monkeypatch)
    document = db.query(KnowledgeDocument).one()
    rows = (
        db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.chunk_type != "parent")
        .order_by(KnowledgeChunk.chunk_index)
        .all()
    )
    assert len(rows) >= 2
    # Establish a real source-span parent to exercise deletion invalidation even for a tiny fixture.
    parent = KnowledgeChunk(
        document_id=document.id,
        tenant_id=document.tenant_id,
        source_type="教材",
        source_name="fixture",
        content=document.normalized_text,
        chunk_type="parent",
        embedding=None,
        content_start=0,
        content_end=len(document.normalized_text),
        section_path=rows[0].section_path,
        page_no=rows[0].page_no,
        meta={},
    )
    db.add(parent)
    db.flush()
    for row in rows[:2]:
        row.parent_chunk_id = parent.id
        row.chunk_type = "child"
    db.commit()
    parent_id = parent.id
    victim = rows[0].id
    remaining = rows[1].id
    KnowledgeService(db).delete_knowledge(victim, operations_runtime[1])
    db.expire_all()
    assert db.get(KnowledgeChunk, parent_id) is None and db.get(KnowledgeChunk, victim) is None
    survivor = db.get(KnowledgeChunk, remaining)
    assert (
        survivor is not None
        and survivor.parent_chunk_id is None
        and survivor.chunk_type == "single"
    )
    assert db.query(KnowledgeDocument).one().status == "partial_index"
    assert db.get(OcrJob, identifier).index_status == "stale"
    texts, citations = expand(
        db, [Candidate(survivor, similarity=1)], KnowledgeScope(), document.tenant_id, False, 1
    )
    assert all(c["chunk_id"] != parent_id for c in citations)


def test_parent_cannot_be_deleted_via_single_leaf_api(db, operations_runtime, monkeypatch):
    identifier, _ = indexed(db, operations_runtime, monkeypatch)
    document = db.query(KnowledgeDocument).one()
    parent = KnowledgeChunk(
        document_id=document.id,
        tenant_id=document.tenant_id,
        source_type="教材",
        source_name="fixture",
        content="parent",
        chunk_type="parent",
    )
    db.add(parent)
    db.commit()
    with pytest.raises(InvalidKnowledgeError):
        KnowledgeService(db).delete_knowledge(parent.id, operations_runtime[1])


def test_document_delete_is_blocked_during_paid_rebuild(db, operations_runtime, monkeypatch):
    identifier, _ = indexed(db, operations_runtime, monkeypatch)
    approve(
        db,
        identifier,
        confirmation(
            db, identifier, operations_runtime[1], rebuild_index=True, expected_index_revision=1
        ),
        operations_runtime[1],
    )
    with pytest.raises(InvalidKnowledgeError):
        KnowledgeService(db).delete_document(
            db.query(KnowledgeDocument).one().id, operations_runtime[1]
        )


def test_document_operations_do_not_cross_tenant(db, operations_runtime, monkeypatch):
    from types import SimpleNamespace

    identifier, _ = indexed(db, operations_runtime, monkeypatch)
    other = SimpleNamespace(id=None, role="researcher", tenant_id="other")
    assert KnowledgeService(db).list_documents(1, 20, other)["total"] == 0
    with pytest.raises(TenantScopeDeniedError):
        KnowledgeService(db).delete_document(db.query(KnowledgeDocument).one().id, other)


def test_no_rebuild_does_not_overwrite_existing_document_or_repay(
    db, operations_runtime, monkeypatch
):
    identifier, calls = indexed(db, operations_runtime, monkeypatch)
    count = len(calls)
    approve(
        db, identifier, confirmation(db, identifier, operations_runtime[1]), operations_runtime[1]
    )
    assert (
        run_index(identifier, operations_runtime[2])["status"] == "not_claimed"
        and len(calls) == count
    )


def test_eval_does_not_credit_same_page_unit_twice():
    from app.versioning import hash_value
    from scripts.rag_eval import Case, score

    case = Case(
        id="fixture",
        query="noun",
        relevant=[{"document_id": "a", "pages": [1], "required_terms": ["Alice"]}],
    )

    def citation(doc, page, text):
        return {
            "document_id": doc,
            "page_no": page,
            "content": text,
            "content_hash": hash_value(text),
        }

    result = score(
        case, [citation("wrong", 1, "Alice"), citation("a", 1, "Alice"), citation("a", 1, "Alice")]
    )
    assert result["page_mrr"] == 0.5 and result["relevant_unit_recall"] == 1
    assert 0 < result["page_ndcg"] < 1 and result["snapshot_hash_valid"]
    assert score(case, [citation("wrong", 1, "Alice")])["terms_in_supporting_context"] is False
