"""Real OCR schema/CAS/checkpoint behavior, scoped to uniquely owned test jobs only."""

from __future__ import annotations

import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import OcrJob
from app.services.ocr_service import OcrService, now
from app.worker.ocr_runner import claim, owns, run_job, sweep
from tests.test_ocr_jobs import FakeClient
from tests.test_rag_ocr import pdf

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(pg_engine, tmp_path, monkeypatch):
    tenant = "ocr-test-" + uuid.uuid4().hex
    monkeypatch.setattr(settings, "RAG_OCR_STORAGE_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "mineru")
    monkeypatch.setattr(settings, "RAG_OCR_URL", "http://localhost:16580")
    FakeClient.calls, FakeClient.version = [], "4.0.10"
    factory = sessionmaker(bind=pg_engine)
    actor = SimpleNamespace(id=None, tenant_id=tenant, role="admin")
    yield factory, actor
    with factory() as db:
        db.query(OcrJob).filter(OcrJob.tenant_id == tenant).delete(synchronize_session=False)
        db.commit()  # Cascades only this fixture's page rows; never truncates a reused DB.


def make(factory, actor, kinds=("scan",)):
    with factory() as db:
        return OcrService(db, lambda _: None).create(io.BytesIO(pdf(kinds)), "course.pdf", actor)


def test_migrated_ocr_tables_have_real_jsonb_fk_and_unique_page_constraint(pg_engine):
    schema = inspect(pg_engine)
    assert {"ocr_job", "ocr_page"} <= set(schema.get_table_names())
    assert any(key["referred_table"] == "ocr_job" for key in schema.get_foreign_keys("ocr_page"))
    assert any(
        key["column_names"] == ["job_id", "page_no"]
        for key in schema.get_unique_constraints("ocr_page")
    )
    assert any(
        column["name"] == "attempt_history" and str(column["type"]) == "JSONB"
        for column in schema.get_columns("ocr_page")
    )


def test_independent_postgres_claims_and_expiry_fence_old_owner(runtime):
    factory, actor = runtime
    identifier = make(factory, actor)["id"]
    with factory() as first, factory() as second:
        token = claim(first, identifier)
        assert token is not None and claim(second, identifier) is None
        second.query(OcrJob).filter(OcrJob.id == identifier).update(
            {"lease_until": now() - timedelta(seconds=1)}
        )
        second.commit()
        assert (
            sweep(factory=factory, dispatch=lambda _: None, job_ids=[identifier])["recovered"] == 1
        )
        fresh = claim(second, identifier)
        assert fresh is not None and fresh != token
        assert not owns(first, identifier, token) and owns(second, identifier, fresh)


def test_real_postgres_checkpoint_preview_and_cancellation_resume(runtime):
    factory, actor = runtime
    identifier = make(factory, actor, ["scan", "native"])["id"]
    assert (
        run_job(identifier, factory=factory, client_factory=FakeClient, dispatch=lambda _: None)[
            "status"
        ]
        == "page_completed"
    )
    with factory() as db:
        service = OcrService(db, lambda _: None)
        detail = service.cancel(identifier, actor)
        assert detail["completed_pages"] == 1
        service.resume(identifier, actor)
    assert (
        run_job(identifier, factory=factory, client_factory=FakeClient, dispatch=lambda _: None)[
            "status"
        ]
        == "page_completed"
    )
    with factory() as db:
        service = OcrService(db, lambda _: None)
        detail = service.summary(service.get(identifier, actor), include_pages=True)
        assert detail["status"] == "completed" and detail["pages"][0]["attempts"] == 1
        preview = json.loads(service.preview_path(identifier, actor).read_text(encoding="utf-8"))
        assert preview["indexable"] and not preview["document"]["stats"]["partial_document"]


def test_reviewed_index_commits_job_and_document_atomically_on_postgres(runtime, monkeypatch):
    from app.models import KnowledgeChunk, KnowledgeDocument
    from app.services.ocr_review import ReviewApproval, approve, plan
    from app.worker.ocr_index import run_index

    factory, actor = runtime
    identifier = make(factory, actor)["id"]
    run_job(identifier, factory=factory, client_factory=FakeClient, dispatch=lambda _: None)
    monkeypatch.setattr("app.services.ocr_review.send_index", lambda _: None)
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda texts, **kwargs: [[1.0] + [0.0] * (settings.EMBEDDING_DIM - 1) for _ in texts],
    )
    with factory() as db:
        preview = plan(db, identifier, actor)
        approval = ReviewApproval(
            preview_hash=preview["preview_hash"],
            plan_hash=preview["plan_hash"],
            source_reviewed=True,
            warnings_acknowledged=True,
            paid_embedding_acknowledged=True,
            knowledge_points=["并列句"],
        )
        approve(db, identifier, approval, actor)
    try:
        result = run_index(identifier, factory)
        assert result["status"] == "indexed"
        with factory() as db:
            job = db.get(OcrJob, identifier)
            document = db.get(KnowledgeDocument, job.indexed_document_id)
            assert document.tenant_id == actor.tenant_id
            assert document.meta["ocr_job_id"] == identifier
            assert (
                db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document.id).count()
                > 0
            )
        assert run_index(identifier, factory)["status"] == "not_claimed"
    finally:
        # Delete only the explicitly owned mock index, not any user knowledge.
        with factory() as db:
            job = db.get(OcrJob, identifier)
            if job and job.indexed_document_id:
                document_id = job.indexed_document_id
                db.query(OcrJob).filter(OcrJob.id == identifier).update(
                    {"indexed_document_id": None}
                )
                db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).delete(
                    synchronize_session=False
                )
                db.query(KnowledgeDocument).filter(KnowledgeDocument.id == document_id).delete(
                    synchronize_session=False
                )
                db.commit()


def test_real_pg_rebuild_keeps_id_and_document_withdrawal_resets_ocr(runtime, monkeypatch):
    from app.models import KnowledgeChunk, KnowledgeDocument
    from app.services.knowledge_service import KnowledgeService
    from app.services.ocr_review import ReviewApproval, approve, plan
    from app.worker.ocr_index import run_index

    factory, actor = runtime
    identifier = make(factory, actor)["id"]
    run_job(identifier, factory=factory, client_factory=FakeClient, dispatch=lambda _: None)
    monkeypatch.setattr("app.services.ocr_review.send_index", lambda _: None)
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda texts, **kwargs: [[1.0] + [0.0] * (settings.EMBEDDING_DIM - 1) for _ in texts],
    )
    document_id = None
    try:
        with factory() as db:
            preview = plan(db, identifier, actor)
            body = ReviewApproval(
                preview_hash=preview["preview_hash"],
                plan_hash=preview["plan_hash"],
                source_reviewed=True,
                warnings_acknowledged=True,
                paid_embedding_acknowledged=True,
            )
            approve(db, identifier, body, actor)
        result = run_index(identifier, factory)
        assert result["status"] == "indexed"
        document_id = result["document_id"]
        with factory() as db:
            body.rebuild_index = True
            body.expected_index_revision = 1
            approve(db, identifier, body, actor)
            old_ids = {
                row.id for row in db.query(KnowledgeChunk).filter_by(document_id=document_id)
            }
        result = run_index(identifier, factory)
        assert result["document_id"] == document_id
        with factory() as db:
            document = db.get(KnowledgeDocument, document_id)
            assert document.meta["index_revision"] == 2
            assert not old_ids & {
                row.id for row in db.query(KnowledgeChunk).filter_by(document_id=document_id)
            }
            parent = KnowledgeChunk(
                document_id=document_id,
                tenant_id=document.tenant_id,
                source_type="教材",
                source_name="fixture",
                content="parent",
                chunk_type="parent",
            )
            db.add(parent)
            db.flush()
            child = (
                db.query(KnowledgeChunk)
                .filter_by(document_id=document_id)
                .filter(KnowledgeChunk.chunk_type != "parent")
                .first()
            )
            child.parent_chunk_id = parent.id
            child.chunk_type = "child"
            db.commit()
            expected_count = db.query(KnowledgeChunk).filter_by(document_id=document_id).count()
            result = KnowledgeService(db).delete_document(document_id, actor)
            assert result["ocr_source_retained"]
            assert result["deleted_chunks"] == expected_count
            db.expire_all()
            job = db.get(OcrJob, identifier)
            assert (
                job.index_status == "removed"
                and job.indexed_document_id is None
                and job.preview_key
            )
    finally:
        if document_id:
            with factory() as db:
                db.query(OcrJob).filter_by(id=identifier).update({"indexed_document_id": None})
                db.query(KnowledgeChunk).filter_by(document_id=document_id).delete(
                    synchronize_session=False
                )
                db.query(KnowledgeDocument).filter_by(id=document_id).delete(
                    synchronize_session=False
                )
                db.commit()
