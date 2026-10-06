"""Approval, original-page QA and atomic reviewed indexing without real paid calls."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.errors import InvalidKnowledgeError, TenantScopeDeniedError
from app.models import KnowledgeChunk, KnowledgeDocument, OcrJob
from app.rag.ocr.render import render_page
from app.services.ocr_review import ReviewApproval, approve, plan
from app.services.ocr_service import OcrService, now
from app.worker.ocr_index import run_index
from app.worker.ocr_runner import run_job, sweep
from tests.test_ocr_jobs import FakeClient
from tests.test_rag_ocr import pdf


@pytest.fixture
def runtime(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_STORAGE_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "mineru")
    monkeypatch.setattr(settings, "RAG_OCR_URL", "http://localhost:16580")
    monkeypatch.setattr("app.services.ocr_review.send_index", lambda _: None)
    owner = SimpleNamespace(id=None, tenant_id="owner", role="admin", status="active")
    factory = sessionmaker(bind=db.get_bind())
    service = OcrService(db, lambda _: None)
    return service, owner, factory


def ready(db, runtime, partial=False):
    service, owner, factory = runtime
    job = service.create(
        io.BytesIO(pdf(["scan", "native"] if partial else ["scan"])),
        "course.pdf",
        owner,
        [1] if partial else None,
    )
    run_job(job["id"], factory=factory, client_factory=FakeClient, dispatch=lambda _: None)
    return job["id"]


def confirmation(db, identifier, owner, **kwargs):
    result = plan(db, identifier, owner, kwargs.get("excluded_block_ids"))
    return ReviewApproval(
        preview_hash=result["preview_hash"],
        plan_hash=result["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        **kwargs,
    )


def vectors(monkeypatch, calls):
    def embed(texts, **kwargs):
        calls.append((texts, kwargs))
        return [[1.0] + [0.0] * (settings.EMBEDDING_DIM - 1) for _ in texts]

    monkeypatch.setattr("app.rag.indexer.embed_texts", embed)


def test_review_plan_never_embeds_and_has_pinned_version_and_inputs(db, runtime, monkeypatch):
    identifier = ready(db, runtime)

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview is not paid")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    result = plan(db, identifier, runtime[1])
    assert len(result["plan_hash"]) == len(result["preview_hash"]) == 64
    assert result["embedding"]["text_count"] == len(result["chunks"])
    assert db.query(KnowledgeDocument).count() == 0


@pytest.mark.parametrize(
    "field", ["source_reviewed", "warnings_acknowledged", "paid_embedding_acknowledged"]
)
def test_all_review_and_paid_gates_are_code_enforced(db, runtime, field):
    identifier = ready(db, runtime)
    body = confirmation(db, identifier, runtime[1])
    setattr(body, field, False)
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, body, runtime[1])
    assert db.get(OcrJob, identifier).index_status == "not_requested"


def test_stale_hash_and_changed_chunk_plan_are_rejected(db, runtime, monkeypatch):
    identifier = ready(db, runtime)
    body = confirmation(db, identifier, runtime[1])
    body.preview_hash = "0" * 64
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, body, runtime[1])
    body = confirmation(db, identifier, runtime[1])
    monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", 1000)
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, body, runtime[1])


def test_partial_selection_requires_own_gate_and_keeps_true_partial_metadata(
    db, runtime, monkeypatch
):
    identifier = ready(db, runtime, True)
    body = confirmation(db, identifier, runtime[1])
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, body, runtime[1])
    calls = []
    vectors(monkeypatch, calls)
    body.accept_selected_pages = True
    approve(db, identifier, body, runtime[1])
    assert run_index(identifier, runtime[2])["status"] == "indexed"
    document = db.query(KnowledgeDocument).one()
    assert document.stats["partial_document"] is True
    assert document.stats["reviewed_index_scope"] == "selected_pages"
    assert document.meta["source_verified"] is True


def test_double_approval_and_duplicate_message_do_not_embed_twice(db, runtime, monkeypatch):
    identifier = ready(db, runtime)
    body = confirmation(db, identifier, runtime[1], knowledge_points=["并列句"])
    calls = []
    vectors(monkeypatch, calls)
    approve(db, identifier, body, runtime[1])
    approve(db, identifier, body, runtime[1])
    assert run_index(identifier, runtime[2])["status"] == "indexed"
    count = len(calls)
    assert run_index(identifier, runtime[2])["status"] == "not_claimed"
    assert len(calls) == count and db.query(KnowledgeDocument).count() == 1
    assert db.get(OcrJob, identifier).indexed_document_id == db.query(KnowledgeDocument).one().id
    assert all(row.knowledge_point_labels for row in db.query(KnowledgeChunk))


def test_excluded_blocks_change_plan_and_never_embed_removed_content(db, runtime, monkeypatch):
    identifier = ready(db, runtime)
    original = plan(db, identifier, runtime[1])
    table = next(
        block for block in original["document"]["blocks"] if block["block_type"] == "table"
    )
    body = confirmation(db, identifier, runtime[1], excluded_block_ids=[table["block_id"]])
    assert body.plan_hash != original["plan_hash"]
    calls = []
    vectors(monkeypatch, calls)
    approve(db, identifier, body, runtime[1])
    run_index(identifier, runtime[2])
    assert all("Alice" not in text for batch, _ in calls for text in batch)
    assert db.query(KnowledgeDocument).one().stats["excluded_block_ids"] == [table["block_id"]]
    with pytest.raises(InvalidKnowledgeError):
        plan(db, identifier, runtime[1], ["unknown"])


def test_worker_plan_drift_refuses_before_any_paid_call(db, runtime, monkeypatch):
    identifier = ready(db, runtime)
    approve(db, identifier, confirmation(db, identifier, runtime[1]), runtime[1])
    calls = []
    vectors(monkeypatch, calls)
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_NAME", "different-configured-model")
    assert run_index(identifier, runtime[2])["status"] == "needs_attention"
    assert not calls and db.query(KnowledgeDocument).count() == 0


def test_failed_paid_attempt_is_not_auto_retried_and_writes_no_half_document(
    db, runtime, monkeypatch
):
    identifier = ready(db, runtime)
    approve(db, identifier, confirmation(db, identifier, runtime[1]), runtime[1])

    def invalid(texts, **kwargs):
        return [[float("nan")] * settings.EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr("app.rag.indexer.embed_texts", invalid)
    assert run_index(identifier, runtime[2])["status"] == "needs_attention"
    assert db.query(KnowledgeDocument).count() == db.query(KnowledgeChunk).count() == 0
    with pytest.raises(InvalidKnowledgeError):
        approve(db, identifier, confirmation(db, identifier, runtime[1]), runtime[1])
    calls = []
    vectors(monkeypatch, calls)
    approve(
        db, identifier, confirmation(db, identifier, runtime[1], retry_authorized=True), runtime[1]
    )
    assert run_index(identifier, runtime[2])["status"] == "indexed"


def test_expired_paid_lease_requires_manual_attention_not_scheduler_replay(db, runtime):
    identifier = ready(db, runtime)
    db.query(OcrJob).filter(OcrJob.id == identifier).update(
        {
            "index_status": "indexing",
            "index_token": str(uuid.uuid4()),
            "index_until": now() - timedelta(seconds=1),
        }
    )
    db.commit()
    sweep(factory=runtime[2], dispatch=lambda _: None, job_ids=[identifier])
    db.refresh(db.get(OcrJob, identifier))
    assert db.get(OcrJob, identifier).index_status == "needs_attention"


def test_tenant_scope_protects_review_and_approval(db, runtime):
    identifier = ready(db, runtime)
    other = SimpleNamespace(id=None, role="researcher", tenant_id="other")
    with pytest.raises(TenantScopeDeniedError):
        plan(db, identifier, other)
    with pytest.raises(TenantScopeDeniedError):
        approve(db, identifier, confirmation(db, identifier, runtime[1]), other)


def test_original_page_renderer_is_bounded_and_does_not_modify_source(db, runtime):
    identifier = ready(db, runtime)
    job = db.get(OcrJob, identifier)
    path = runtime[0].storage.path(job.input_key)
    original = path.read_bytes()
    png, size = render_page(path, 1)
    assert png.startswith(b"\x89PNG") and size == (400, 600)
    assert path.read_bytes() == original
    with pytest.raises(ValueError):
        render_page(path, 99)


def test_provider_batch_cap_is_respected_for_reviewed_index(db, runtime, monkeypatch):
    identifier = ready(db, runtime)
    approve(db, identifier, confirmation(db, identifier, runtime[1]), runtime[1])
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER_BATCH_LIMIT", 1)
    calls = []
    vectors(monkeypatch, calls)
    assert run_index(identifier, runtime[2])["status"] == "indexed"
    assert all(len(batch) == 1 for batch, _ in calls)


def test_production_renderer_declares_both_pdfium_and_png_dependencies():
    from pathlib import Path

    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "pypdfium2>=" in requirements and "Pillow>=" in requirements


def test_retrieval_distinguishes_reviewed_source_from_unverified_facts(db, runtime, monkeypatch):
    from app.rag.retrieval.context import citation
    from app.rag.retrieval.models import Candidate

    identifier = ready(db, runtime)
    approve(db, identifier, confirmation(db, identifier, runtime[1]), runtime[1])
    calls = []
    vectors(monkeypatch, calls)
    run_index(identifier, runtime[2])
    row = db.query(KnowledgeChunk).filter(KnowledgeChunk.embedding.is_not(None)).first()
    result = citation(row, Candidate(row))
    assert result["source_reviewed"] is True
    assert result["source_review_job_id"] == identifier
    assert result["verification"] == "unverified"
