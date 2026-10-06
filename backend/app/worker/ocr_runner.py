"""One page per broker message, durable SQL leases/fencing/checkpoints and periodic recovery."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.errors import DocumentParseError, InvalidKnowledgeError
from app.models import OcrJob, OcrPage
from app.rag.ocr.assemble import assemble
from app.rag.ocr.mineru import MinerUClient, OcrParseError
from app.rag.ocr.pipeline import parse_pdf_with_ocr
from app.rag.ocr.storage import OcrStorage
from app.rag.preview import preview_document
from app.services.ocr_service import enqueue, now, send_job


class Cancelled(DocumentParseError):
    pass


def document_digest(document: Any) -> str:
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def claim(db: Session, job_id: str) -> str | None:
    token, instant = str(uuid.uuid4()), now()
    changed = (
        db.query(OcrJob)
        .filter(
            OcrJob.id == job_id,
            OcrJob.cancel_requested_at.is_(None),
            or_(
                OcrJob.status == "pending",
                and_(OcrJob.status == "retry", OcrJob.retry_after <= instant),
            ),
        )
        .update(
            {
                "status": "running",
                "lease_token": token,
                "lease_until": instant
                + timedelta(
                    seconds=max(settings.RAG_OCR_LEASE_SECONDS, settings.RAG_OCR_PAGE_TIMEOUT + 120)
                ),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return token if changed else None


def owns(db: Session, job_id: str, token: str) -> bool:
    return (
        db.query(OcrJob)
        .filter(
            OcrJob.id == job_id,
            OcrJob.lease_token == token,
            OcrJob.status == "running",
            OcrJob.cancel_requested_at.is_(None),
        )
        .count()
        == 1
    )


def finalize(db: Session, job: OcrJob, token: str, storage: OcrStorage) -> bool:
    pages = db.query(OcrPage).filter(OcrPage.job_id == job.id).order_by(OcrPage.page_no).all()
    checkpoints: list[tuple[str, str]] = []
    for page in pages:
        if page.status != "completed" or not page.result_key or not page.result_hash:
            raise DocumentParseError("OCR 页检查点尚未全部完成")
        checkpoints.append((page.result_key, page.result_hash))
    if not checkpoints:
        raise DocumentParseError("OCR 文档没有页检查点")
    total_bytes = sum(storage.path(key).stat().st_size for key, _ in checkpoints)
    if total_bytes > settings.RAG_OCR_MAX_PREVIEW_BYTES:
        raise DocumentParseError("全文件检查点超过预览上限，需受控批次或调整配置")
    documents = [storage.read_json(key, digest) for key, digest in checkpoints]
    document = assemble(job.filename, job.source_hash, job.page_count, documents, job.id)
    result = preview_document(document)
    key = f"jobs/{job.id}/preview-{token}.json"
    digest = storage.write_json(key, result)
    changed = (
        db.query(OcrJob)
        .filter(
            OcrJob.id == job.id, OcrJob.lease_token == token, OcrJob.cancel_requested_at.is_(None)
        )
        .update(
            {
                "status": "completed",
                "preview_key": key,
                "preview_hash": digest,
                "lease_token": None,
                "lease_until": None,
                "error_code": None,
                "error_message": None,
            },
            synchronize_session=False,
        )
    )
    if not changed:
        db.rollback()
        return False
    db.commit()
    return True


def run_job(
    job_id: str,
    *,
    factory: Callable[[], Session] = SessionLocal,
    client_factory: Callable[..., Any] = MinerUClient,
    dispatch: Callable[[str], None] = send_job,
) -> dict[str, Any]:
    db, storage = factory(), OcrStorage()
    page = None
    token = None
    try:
        token = claim(db, job_id)
        if token is None:
            return {"job_id": job_id, "status": "not_claimed"}
        job = db.get(OcrJob, job_id)
        assert job is not None
        page = (
            db.query(OcrPage)
            .filter(OcrPage.job_id == job_id, OcrPage.status == "pending")
            .order_by(OcrPage.page_no)
            .first()
        )
        if page is None:
            finished = finalize(db, job, token, storage)
            return {"job_id": job_id, "status": "completed" if finished else "cancelled_or_stale"}
        page.status, page.attempts = "running", page.attempts + 1
        page.attempt_history = [
            *(page.attempt_history or []),
            {
                "trace_id": f"ocr:{job_id}:{page.page_no}:{token}",
                "status": "running",
                "attempt": page.attempts,
                "started_at": now().isoformat(),
            },
        ]
        db.commit()
        started = time.monotonic()
        last_check, last_cancelled = 0.0, False

        def cancelled() -> bool:
            nonlocal last_check, last_cancelled
            instant = time.monotonic()
            if instant - last_check >= 0.5:
                session = factory()
                try:
                    last_cancelled = not owns(session, job_id, token)
                finally:
                    session.close()
                last_check = instant
            return last_cancelled

        content = storage.input_bytes(job.input_key, job.source_hash)
        client = client_factory(cancel_check=cancelled)
        version = client.server_version()
        cache_key = storage.cache_key(job.tenant_id, job.source_hash, page.page_no, version)
        document, cache_hit = None, False
        if storage.path(cache_key).is_file():
            try:
                cached = storage.read_json(cache_key)
                if cached["digest"] == document_digest(cached["document"]):
                    document = cached["document"]
                    if document["source_hash"] != job.source_hash or document["stats"][
                        "selected_pages"
                    ] != [page.page_no]:
                        document = None
                    cache_hit = document is not None
            except (OSError, ValueError, KeyError, TypeError):
                document = (
                    None  # Reparse corrupt cache; committed checkpoints are separately verified.
                )
        if document is None:
            document = parse_pdf_with_ocr(
                job.filename, content, page_numbers=[page.page_no], client=client
            )
            versions = {
                route.get("ocr_version")
                for route in document["stats"]["page_routes"]
                if route["route"] == "ocr"
            }
            if versions and versions != {version}:
                raise OcrParseError("schema", "处理期间引擎版本改变，不能写入旧版本缓存")
            if cancelled():
                raise Cancelled("OCR任务已取消或所有权已变化")
            storage.write_json(
                cache_key, {"digest": document_digest(document), "document": document}
            )
        document["source_name"] = Path(job.filename).stem
        document["stats"]["cache_hit"] = cache_hit
        if cache_hit:
            for route in document["stats"]["page_routes"]:
                route["cache_hit"] = True
                route["cached_original_latency_seconds"] = route.pop("ocr_latency_seconds", None)
        key = f"jobs/{job_id}/page-{page.page_no}-{token}.json"
        digest = storage.write_json(key, document)
        # Fence before touching the page row. Never overwrite a newer attempt's committed output.
        changed = (
            db.query(OcrJob)
            .filter(
                OcrJob.id == job_id,
                OcrJob.lease_token == token,
                OcrJob.cancel_requested_at.is_(None),
            )
            .update(
                {
                    "status": "running",
                    "updated_at": now(),
                },
                synchronize_session=False,
            )
        )
        if not changed:
            db.rollback()
            return {"job_id": job_id, "status": "stale"}
        page.status, page.result_key, page.result_hash = "completed", key, digest
        page.cache_hit, page.engine_version = cache_hit, version
        page.latency_seconds = round(time.monotonic() - started, 3)
        page.error_code, page.error_message = None, None
        page.attempt_history = [
            *page.attempt_history[:-1],
            {
                **page.attempt_history[-1],
                "status": "completed",
                "completed_at": now().isoformat(),
                "cache_hit": cache_hit,
                "engine_version": version,
                "latency_seconds": page.latency_seconds,
                "parse_trace_id": document["stats"].get("parse_trace_id"),
            },
        ]
        db.commit()
        remaining = (
            db.query(OcrPage)
            .filter(OcrPage.job_id == job_id, OcrPage.status != "completed")
            .count()
        )
        if not remaining:
            finalize(db, job, token, storage)
        else:
            db.query(OcrJob).filter(OcrJob.id == job_id, OcrJob.lease_token == token).update(
                {
                    "status": "pending",
                    "lease_token": None,
                    "lease_until": None,
                    "dispatch_after": None,
                    "retry_after": None,
                },
                synchronize_session=False,
            )
            db.commit()
            enqueue(db, job_id, dispatch)
        return {
            "job_id": job_id,
            "page_no": page.page_no,
            "status": "page_completed",
            "cache_hit": cache_hit,
        }
    except (
        DocumentParseError,
        InvalidKnowledgeError,
        ValueError,
        OSError,
        SoftTimeLimitExceeded,
    ) as exc:
        db.rollback()
        if token is None or not owns(db, job_id, token):
            return {"job_id": job_id, "status": "cancelled_or_stale"}
        cancelled_error = (
            isinstance(exc, Cancelled)
            or isinstance(exc, OcrParseError)
            and exc.stage == "cancelled"
        )
        transient = (
            isinstance(exc, (OSError, SoftTimeLimitExceeded))
            or isinstance(exc, OcrParseError)
            and exc.stage not in {"input", "config", "schema", "routing", "cancelled"}
        )
        retry = transient and page is not None and page.attempts < settings.RAG_OCR_MAX_ATTEMPTS
        message = (
            str(exc)[:500]
            if isinstance(exc, (DocumentParseError, InvalidKnowledgeError))
            else "OCR 本地文件/任务处理失败"
        )
        code = "OCR_CANCELLED" if cancelled_error else "OCR_RETRY" if retry else "OCR_FAILED"
        # Conditional update also locks the owner row before page failure updates.
        changed = (
            db.query(OcrJob)
            .filter(OcrJob.id == job_id, OcrJob.lease_token == token)
            .update(
                {
                    "status": "cancelled" if cancelled_error else "retry" if retry else "failed",
                    "lease_token": None,
                    "lease_until": None,
                    "error_code": code,
                    "error_message": message,
                    "retry_after": (
                        now() + timedelta(seconds=settings.RAG_OCR_RETRY_SECONDS) if retry else None
                    ),
                    "dispatch_after": (
                        now() + timedelta(seconds=settings.RAG_OCR_RETRY_SECONDS) if retry else None
                    ),
                },
                synchronize_session=False,
            )
        )
        if not changed:
            db.rollback()
            return {"job_id": job_id, "status": "stale"}
        if page is not None and page.status != "completed":
            page.status = "pending" if retry else "cancelled" if cancelled_error else "failed"
            page.error_code, page.error_message = code, message
            if page.attempt_history:
                page.attempt_history = [
                    *page.attempt_history[:-1],
                    {
                        **page.attempt_history[-1],
                        "status": page.status,
                        "completed_at": now().isoformat(),
                        "error_code": code,
                        "error_message": message,
                    },
                ]
        db.commit()
        return {
            "job_id": job_id,
            "status": "retry" if retry else "cancelled" if cancelled_error else "failed",
        }
    finally:
        db.close()


def sweep(
    *,
    factory: Callable[[], Session] = SessionLocal,
    dispatch: Callable[[str], None] = send_job,
    job_ids: list[str] | None = None,
) -> dict[str, int]:
    db = factory()
    recovered = sent = 0
    try:
        query = db.query(OcrJob)
        if job_ids is not None:
            query = query.filter(OcrJob.id.in_(job_ids))
        expired = query.filter(OcrJob.status == "running", OcrJob.lease_until <= now()).all()
        for job in expired:
            old = job.lease_token
            changed = (
                db.query(OcrJob)
                .filter(OcrJob.id == job.id, OcrJob.lease_token == old, OcrJob.lease_until <= now())
                .update(
                    {
                        "status": "pending",
                        "lease_token": None,
                        "lease_until": None,
                        "dispatch_after": None,
                    },
                    synchronize_session=False,
                )
            )
            if not changed:
                continue
            for page in db.query(OcrPage).filter(
                OcrPage.job_id == job.id, OcrPage.status == "running"
            ):
                page.status = (
                    "failed" if page.attempts >= settings.RAG_OCR_MAX_ATTEMPTS else "pending"
                )
                page.error_code, page.error_message = (
                    "OCR_LEASE_EXPIRED",
                    "worker失联，已回收页租约",
                )
                if page.attempt_history:
                    page.attempt_history = [
                        *page.attempt_history[:-1],
                        {
                            **page.attempt_history[-1],
                            "status": "lease_expired",
                            "completed_at": now().isoformat(),
                        },
                    ]
                if page.status == "failed":
                    db.query(OcrJob).filter(OcrJob.id == job.id).update(
                        {"status": "failed", "error_code": "OCR_RETRIES_EXHAUSTED"},
                        synchronize_session=False,
                    )
            recovered += 1
        db.commit()
        jobs = (
            query.filter(
                OcrJob.status.in_(["pending", "retry"]),
                or_(OcrJob.dispatch_after.is_(None), OcrJob.dispatch_after <= now()),
            )
            .order_by(OcrJob.updated_at)
            .limit(50)
            .all()
        )
        for job in jobs:
            sent += enqueue(db, job.id, dispatch)
        from app.services.ocr_review import dispatch_index

        index_query = db.query(OcrJob)
        if job_ids is not None:
            index_query = index_query.filter(OcrJob.id.in_(job_ids))
        # Paid attempts with an expired owner are never silently replayed.
        index_query.filter(OcrJob.index_status == "indexing", OcrJob.index_until <= now()).update(
            {
                "index_status": "needs_attention",
                "index_error": "索引worker失联，费用状态不确定；请核对后显式重试",
                "index_token": None,
                "index_until": None,
            },
            synchronize_session=False,
        )
        db.commit()
        for pending in index_query.filter(OcrJob.index_status == "pending").limit(50):
            dispatch_index(db, pending.id)
        return {"recovered": recovered, "sent": sent}
    finally:
        db.close()
