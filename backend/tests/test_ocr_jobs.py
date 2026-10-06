"""Durable page tasks, cancellation, caches and delivery recovery without real OCR or billing."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from kombu.exceptions import OperationalError as BrokerError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.errors import DocumentParseError, InvalidKnowledgeError, TenantScopeDeniedError
from app.models import KnowledgeChunk, KnowledgeDocument, OcrJob, OcrPage
from app.rag.ocr.mineru import OcrParseError, validate_native
from app.rag.ocr.storage import OcrStorage
from app.services.ocr_service import OcrService, enqueue, now
from app.worker.ocr_runner import claim, owns, run_job, sweep
from tests.test_rag_ocr import native, pdf


class FakeClient:
    calls = []
    version = "4.0.10"

    def __init__(self, cancel_check=None):
        self.cancel_check = cancel_check

    def server_version(self):
        return self.version

    def parse_page(self, content, filename="page.pdf"):
        self.calls.append(filename)
        return validate_native(native())


@pytest.fixture(autouse=True)
def local_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_STORAGE_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(settings, "RAG_OCR_IMPORT_ROOT", str(tmp_path / "imports"))
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "mineru")
    monkeypatch.setattr(settings, "RAG_OCR_URL", "http://localhost:16580")
    FakeClient.calls, FakeClient.version = [], "4.0.10"

    def forbidden(*args, **kwargs):
        raise AssertionError("OCR jobs must never index/embeddings")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)


@pytest.fixture
def owner():
    return SimpleNamespace(id=None, tenant_id="a", role="admin", status="active")


def service(db):
    return OcrService(db, dispatch=lambda job_id: None)


def execute(db, job_id, client=FakeClient):
    factory = sessionmaker(bind=db.get_bind())
    return run_job(job_id, factory=factory, client_factory=client, dispatch=lambda _: None)


def create(db, owner, kinds=("scan",), pages=None):
    return service(db).create(io.BytesIO(pdf(kinds)), "course.pdf", owner, pages)


def test_job_acceptance_is_durable_and_no_inference_or_indexing(db, owner):
    result = create(db, owner, ["scan", "native", "blank"])
    assert result["status"] == "pending" and result["total_pages"] == 3
    assert db.query(OcrPage).count() == 3 and FakeClient.calls == []
    assert db.query(KnowledgeChunk).count() == db.query(KnowledgeDocument).count() == 0
    assert "input_key" not in result and not result["ready_for_indexing"]


def test_page_tasks_checkpoint_then_assemble_complete_preview(db, owner):
    result = create(db, owner, ["scan", "native", "blank"])
    for count in (1, 2, 3):
        assert execute(db, result["id"])["status"] == "page_completed"
        detail = service(db).summary(db.get(OcrJob, result["id"]), include_pages=True)
        assert detail["completed_pages"] == count
    assert detail["status"] == "completed" and detail["preview_available"]
    assert FakeClient.calls == ["page-1.pdf"]
    preview = json.loads(service(db).preview_path(result["id"], owner).read_text(encoding="utf-8"))
    assert not preview["document"]["stats"]["partial_document"] and preview["indexable"]
    if preview["chunk_layout"] == "structure":
        assert all(chunk["content_start"] is None for chunk in preview["chunks"])
        assert all(
            chunk["content"][segment["content_start"] : segment["content_end"]]
            == preview["document"]["text"][segment["source_start"] : segment["source_end"]]
            for chunk in preview["chunks"]
            for segment in chunk["source_segments"]
        )
    else:
        assert all(
            chunk["content"]
            == preview["document"]["text"][chunk["content_start"] : chunk["content_end"]]
            for chunk in preview["chunks"]
        )
    assert detail["pages"][0]["attempt_history"][0]["trace_id"].startswith("ocr:")
    assert detail["indexing_performed"] is False


def test_partial_selection_completed_job_is_not_full_document_indexable(db, owner):
    result = create(db, owner, ["scan", "native", "scan"], [1, 3])
    execute(db, result["id"])
    execute(db, result["id"])
    preview = json.loads(service(db).preview_path(result["id"], owner).read_text(encoding="utf-8"))
    assert preview["indexable"] is False and preview["reason_code"] == "KNOWLEDGE_PARTIAL_DOCUMENT"
    assert any(
        block["meta"].get("reset_section_context") for block in preview["document"]["blocks"]
    )


@pytest.mark.parametrize("pages", [[1, 1], [2, 1], [0], [9], [], [True]])
def test_invalid_pages_never_create_db_job(db, owner, pages):
    with pytest.raises(InvalidKnowledgeError):
        create(db, owner, ["scan", "native"], pages)
    assert db.query(OcrJob).count() == 0


def test_size_and_job_page_limits_are_separate_from_three_page_cli(db, owner, monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_MAX_JOB_PAGES", 5)
    result = create(db, owner, ["blank"] * 4)
    assert result["total_pages"] == 4
    monkeypatch.setattr(settings, "RAG_OCR_MAX_JOB_PAGES", 1)
    with pytest.raises(InvalidKnowledgeError):
        create(db, owner, ["blank"] * 2)
    monkeypatch.setattr(settings, "RAG_OCR_MAX_INPUT_BYTES", 10)
    with pytest.raises(InvalidKnowledgeError):
        create(db, owner)


def test_bad_pdf_and_disabled_engine_fail_before_queue(db, owner, monkeypatch):
    with pytest.raises(InvalidKnowledgeError):
        service(db).create(io.BytesIO(b"not pdf"), "bad.pdf", owner)
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "off")
    with pytest.raises(InvalidKnowledgeError):
        create(db, owner)


def test_source_snapshot_integrity_is_checked_even_for_cached_jobs(db, owner):
    first = create(db, owner)
    execute(db, first["id"])
    second = create(db, owner)
    job = db.get(OcrJob, second["id"])
    OcrStorage().path(job.input_key).write_bytes(b"corrupt")
    assert execute(db, second["id"])["status"] == "failed"
    assert len(FakeClient.calls) == 1


def test_cache_hits_are_tenant_and_version_isolated(db, owner):
    first = create(db, owner)
    execute(db, first["id"])
    second = create(db, owner)
    execute(db, second["id"])
    detail = service(db).summary(db.get(OcrJob, second["id"]), include_pages=True)
    assert detail["cached_pages"] == 1 and len(FakeClient.calls) == 1
    other = SimpleNamespace(id=None, tenant_id="b", role="admin")
    third = create(db, other)
    execute(db, third["id"])
    assert len(FakeClient.calls) == 2
    FakeClient.version = "4.0.11"

    class NewVersion(FakeClient):
        def parse_page(self, content, filename="page.pdf"):
            self.calls.append(filename)
            data = native()
            data["metadata"]["producer"]["version"] = self.version
            return validate_native(data)

    fourth = create(db, owner)
    execute(db, fourth["id"], NewVersion)
    assert len(FakeClient.calls) == 3


def test_corrupt_cache_reparses_without_trusting_false_results(db, owner):
    first = create(db, owner)
    execute(db, first["id"])
    job = db.get(OcrJob, first["id"])
    key = OcrStorage().cache_key(owner.tenant_id, job.source_hash, 1, "4.0.10")
    OcrStorage().path(key).write_text('{"digest":"wrong","document":{}}', encoding="utf-8")
    second = create(db, owner)
    execute(db, second["id"])
    assert len(FakeClient.calls) == 2
    assert service(db).summary(db.get(OcrJob, second["id"]))["cached_pages"] == 0


def test_duplicate_broker_delivery_cannot_duplicate_successful_page(db, owner):
    result = create(db, owner)
    execute(db, result["id"])
    assert execute(db, result["id"])["status"] == "not_claimed"
    assert FakeClient.calls == ["page-1.pdf"]


def test_cancel_preserves_completed_pages_and_resume_skips_them(db, owner):
    result = create(db, owner, ["scan", "scan"])
    execute(db, result["id"])
    cancelled = service(db).cancel(result["id"], owner)
    assert cancelled["status"] == "cancelled" and cancelled["completed_pages"] == 1
    assert execute(db, result["id"])["status"] == "not_claimed"
    service(db).resume(result["id"], owner)
    execute(db, result["id"])
    detail = service(db).summary(db.get(OcrJob, result["id"]), include_pages=True)
    assert detail["status"] == "completed" and detail["completed_pages"] == 2
    assert len(FakeClient.calls) == 2


def test_inflight_cancel_fences_result_and_keeps_job_cancelled(db, owner):
    result = create(db, owner)

    class CancelClient(FakeClient):
        def parse_page(self, content, filename="page.pdf"):
            service(db).cancel(result["id"], owner)
            return validate_native(native())

    assert execute(db, result["id"], CancelClient)["status"] == "cancelled_or_stale"
    detail = service(db).summary(db.get(OcrJob, result["id"]))
    assert detail["status"] == "cancelled" and detail["completed_pages"] == 0
    assert (
        db.query(OcrPage).filter(OcrPage.job_id == result["id"]).one().attempt_history[-1]["status"]
        == "cancelled"
    )


def test_transient_failure_retries_with_backoff_and_records_attempts(db, owner):
    result = create(db, owner)

    class Fail(FakeClient):
        def parse_page(self, content, filename="page.pdf"):
            raise OcrParseError("poll", "local unavailable")

    assert execute(db, result["id"], Fail)["status"] == "retry"
    assert execute(db, result["id"])["status"] == "not_claimed"
    db.query(OcrJob).filter(OcrJob.id == result["id"]).update(
        {"retry_after": now() - timedelta(seconds=1), "dispatch_after": None}
    )
    db.commit()
    execute(db, result["id"])
    detail = service(db).summary(db.get(OcrJob, result["id"]), include_pages=True)
    assert detail["status"] == "completed" and detail["pages"][0]["attempts"] == 2
    assert len(detail["pages"][0]["attempt_history"]) == 2


def test_retry_exhaustion_and_permanent_failure_are_visible(db, owner, monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_MAX_ATTEMPTS", 1)
    result = create(db, owner)

    class Fail(FakeClient):
        def parse_page(self, content, filename="page.pdf"):
            raise OcrParseError("schema", "bad native contract")

    assert execute(db, result["id"], Fail)["status"] == "failed"
    detail = service(db).summary(db.get(OcrJob, result["id"]), include_pages=True)
    assert detail["pages"][0]["status"] == "failed" and detail["error_code"] == "OCR_FAILED"
    service(db).resume(result["id"], owner)
    execute(db, result["id"])
    assert service(db).summary(db.get(OcrJob, result["id"]))["status"] == "completed"


def test_expired_lease_recovered_and_old_owner_fenced(db, owner):
    result = create(db, owner)
    old = claim(db, result["id"])
    db.query(OcrJob).filter(OcrJob.id == result["id"]).update(
        {"lease_until": now() - timedelta(seconds=1)}
    )
    db.query(OcrPage).filter(OcrPage.job_id == result["id"]).update(
        {"status": "running", "attempts": 1}
    )
    db.commit()
    sent = []
    assert sweep(factory=sessionmaker(bind=db.get_bind()), dispatch=sent.append)["recovered"] == 1
    new = claim(db, result["id"])
    assert new and new != old and not owns(db, result["id"], old)
    assert owns(db, result["id"], new)


def test_unavailable_broker_does_not_lose_committed_job(db, owner):
    def fail(identifier):
        raise BrokerError("offline")

    result = OcrService(db, fail).create(io.BytesIO(pdf(["scan"])), "course.pdf", owner)
    assert db.get(OcrJob, result["id"]).status == "pending"
    db.query(OcrJob).filter(OcrJob.id == result["id"]).update(
        {"dispatch_after": now() - timedelta(seconds=1)}
    )
    db.commit()
    sent = []
    assert sweep(factory=sessionmaker(bind=db.get_bind()), dispatch=sent.append)["sent"] == 1
    assert sent == [result["id"]]


def test_resume_repairs_corrupt_checkpoint_without_discarding_other_pages(db, owner):
    result = create(db, owner, ["scan", "scan"])
    execute(db, result["id"])
    checkpoint = (
        db.query(OcrPage).filter(OcrPage.job_id == result["id"], OcrPage.page_no == 1).one()
    )
    OcrStorage().path(checkpoint.result_key).write_bytes(b"corrupt")
    service(db).cancel(result["id"], owner)
    service(db).resume(result["id"], owner)
    execute(db, result["id"])
    execute(db, result["id"])
    assert service(db).summary(db.get(OcrJob, result["id"]))["status"] == "completed"
    assert len(FakeClient.calls) == 2  # repaired page reused its valid original cache


def test_completed_task_cannot_be_resumed(db, owner):
    result = create(db, owner)
    execute(db, result["id"])
    with pytest.raises(InvalidKnowledgeError):
        service(db).resume(result["id"], owner)


def test_tenant_is_enforced_for_details_cancel_resume_preview_and_list(db, owner):
    result = create(db, owner)
    other = SimpleNamespace(id=None, tenant_id="other", role="researcher")
    assert service(db).list_jobs(other, 1, 20)["total"] == 0
    for method in [
        service(db).get,
        service(db).cancel,
        service(db).resume,
        service(db).preview_path,
    ]:
        with pytest.raises(TenantScopeDeniedError):
            method(result["id"], other)


def test_local_import_batch_rejects_escape_and_accepts_only_configured_root(db, owner, tmp_path):
    root = Path(settings.RAG_OCR_IMPORT_ROOT)
    root.mkdir()
    (root / "ok.pdf").write_bytes(pdf(["scan"]))
    (tmp_path / "outside.pdf").write_bytes(pdf(["scan"]))
    result = service(db).import_local(["ok.pdf", "../outside.pdf", "missing.pdf"], owner)
    assert len(result["accepted"]) == 1 and len(result["errors"]) == 2


@pytest.mark.parametrize("key", ["../outside", "/outside"])
def test_spool_key_must_stay_within_shared_root(key):
    with pytest.raises(DocumentParseError):
        OcrStorage().path(key)


def test_preview_endpoint_requires_valid_checkpoint(db, owner):
    result = create(db, owner)
    with pytest.raises(InvalidKnowledgeError):
        service(db).preview_path(result["id"], owner)
    execute(db, result["id"])
    path = service(db).preview_path(result["id"], owner)
    path.write_text("bad", encoding="utf-8")
    with pytest.raises(InvalidKnowledgeError):
        service(db).preview_path(result["id"], owner)


def test_api_create_progress_cancel_and_resume_are_authorized_and_no_paid_calls(
    db, owner, monkeypatch
):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import create_app
    from app.security import get_current_user
    from app.worker.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    assert client.get("/api/knowledge/ocr/jobs").status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: owner
    response = client.post(
        "/api/knowledge/ocr/jobs/upload",
        files={"file": ("x.pdf", pdf(["scan"]), "application/pdf")},
    )
    assert response.status_code == 202
    identifier = response.json()["id"]
    assert client.get("/api/knowledge/ocr/jobs/" + identifier).json()["completed_pages"] == 0
    assert (
        client.post("/api/knowledge/ocr/jobs/" + identifier + "/cancel").json()["status"]
        == "cancelled"
    )
    assert client.post("/api/knowledge/ocr/jobs/" + identifier + "/resume").status_code == 202
    assert client.get("/api/knowledge/ocr/jobs/" + identifier + "/preview").status_code == 400


def test_nginx_large_limit_is_only_for_background_ocr_upload():
    text = (Path(__file__).parents[2] / "frontend/nginx.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 11m;" in text
    assert "location = /api/knowledge/ocr/jobs/upload" in text
    assert "client_max_body_size 129m;" in text


def test_delivery_is_throttled_until_dispatch_lease_expires(db, owner):
    result = create(db, owner)
    sent = []
    assert enqueue(db, result["id"], sent.append) is False
    assert not sent
    db.query(OcrJob).filter(OcrJob.id == result["id"]).update(
        {"dispatch_after": now() - timedelta(seconds=1)}
    )
    db.commit()
    assert enqueue(db, result["id"], sent.append) is True
    assert sent == [result["id"]]
