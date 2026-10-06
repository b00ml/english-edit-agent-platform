"""Durable OCR job API operations; no indexing, embeddings or inference in requests."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

from kombu.exceptions import OperationalError as BrokerError
from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import DocumentParseError, InvalidKnowledgeError
from app.models import KnowledgeDocument, OcrJob, OcrPage, User
from app.rag.ocr.mineru import MinerUClient
from app.rag.ocr.storage import OcrStorage
from app.tenancy import require_scope, scope_query

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def send_job(job_id: str) -> None:
    from app.worker.celery_app import celery_app

    celery_app.send_task("app.worker.ocr_tasks.process_ocr_job", args=[job_id], queue="ocr")


def enqueue(db: Session, job_id: str, dispatch: Callable[[str], None] = send_job) -> bool:
    due = now()
    changed = (
        db.query(OcrJob)
        .filter(
            OcrJob.id == job_id,
            OcrJob.status.in_(["pending", "retry"]),
            or_(OcrJob.dispatch_after.is_(None), OcrJob.dispatch_after <= due),
        )
        .update(
            {"dispatch_after": due + timedelta(seconds=settings.RAG_OCR_SWEEP_SECONDS * 2)},
            synchronize_session=False,
        )
    )
    db.commit()
    if not changed:
        return False
    try:
        dispatch(job_id)
    except (BrokerError, OSError, TimeoutError):
        # Job remains in PostgreSQL; periodic sweep will redeliver after dispatch_after.
        logger.warning("OCR dispatch unavailable; durable job %s awaits sweep", job_id)
        return False
    return True


class OcrService:
    def __init__(self, db: Session, dispatch: Callable[[str], None] = send_job) -> None:
        self.db, self.dispatch = db, dispatch
        self.storage = OcrStorage()

    def get(self, job_id: str, user: User) -> OcrJob:
        return require_scope(self.db.get(OcrJob, job_id), user)

    def create(
        self, stream: BinaryIO, filename: str, user: User, pages: list[int] | None = None
    ) -> dict[str, Any]:
        try:
            MinerUClient()  # validate enabled/local configuration; no HTTP request here
            name = Path(filename.replace("\\", "/")).name
            if not name.lower().endswith(".pdf") or not 1 <= len(name) <= 256:
                raise ValueError("需要PDF文件及有效文件名")
            identifier = str(uuid.uuid4())
            key, digest, size = self.storage.save_input(identifier, stream)
            with self.storage.path(key).open("rb") as source:
                reader = PdfReader(source)
                if reader.is_encrypted:
                    raise ValueError("加密PDF需先解密")
                count = len(reader.pages)
            selected = pages if pages is not None else list(range(1, count + 1))
            if (
                not selected
                or len(selected) > settings.RAG_OCR_MAX_JOB_PAGES
                or selected != sorted(set(selected))
                or any(type(number) is not int or not 1 <= number <= count for number in selected)
            ):
                raise ValueError("页码必须有序、不重复、在范围内且不超过后台任务页数上限")
        except (DocumentParseError, PdfReadError, PdfStreamError, ValueError) as exc:
            raise InvalidKnowledgeError(str(exc)) from exc
        except OSError as exc:
            raise InvalidKnowledgeError("OCR 输入无法保存或读取") from exc
        job = OcrJob(
            id=identifier,
            tenant_id=user.tenant_id,
            created_by=user.id,
            filename=name,
            source_hash=digest,
            input_key=key,
            input_bytes=size,
            page_count=count,
            selected_pages=selected,
            status="pending",
        )
        self.db.add(job)
        self.db.flush()
        self.db.add_all(
            OcrPage(job_id=identifier, page_no=number, status="pending", attempts=0)
            for number in selected
        )
        self.db.commit()
        enqueue(self.db, identifier, self.dispatch)
        return self.summary(job)

    def import_local(
        self, files: list[str], user: User, pages: list[int] | None = None
    ) -> dict[str, Any]:
        if not settings.RAG_OCR_IMPORT_ROOT:
            raise InvalidKnowledgeError("未配置本地资料导入根目录")
        root = Path(settings.RAG_OCR_IMPORT_ROOT).resolve()
        accepted, errors = [], []
        for relative in files:
            try:
                candidate = (root / relative).resolve()
                if (
                    Path(relative).is_absolute()
                    or not candidate.is_relative_to(root)
                    or not candidate.is_file()
                ):
                    raise InvalidKnowledgeError("本地资料不在配置的导入范围或不是文件")
                with candidate.open("rb") as source:
                    accepted.append(self.create(source, candidate.name, user, pages))
            except InvalidKnowledgeError as exc:
                errors.append({"file": relative, "code": exc.code, "message": str(exc)})
            except OSError:
                errors.append(
                    {"file": relative, "code": "OCR_INPUT_UNAVAILABLE", "message": "资料不可读取"}
                )
        return {"accepted": accepted, "errors": errors}

    def summary(
        self, job: OcrJob, *, include_pages: bool = False, page: int = 1, page_size: int = 100
    ) -> dict[str, Any]:
        self.db.refresh(job)
        rows = (
            self.db.query(OcrPage).filter(OcrPage.job_id == job.id).order_by(OcrPage.page_no).all()
        )
        counts = Counter(row.status for row in rows)
        indexed_document = (
            self.db.get(KnowledgeDocument, job.indexed_document_id)
            if job.indexed_document_id
            else None
        )
        revision = (
            int((indexed_document.meta or {}).get("index_revision", 1)) if indexed_document else 0
        )
        review_settings = dict(job.approval or {})
        # Existing indexes retain their chosen layout; only never-indexed jobs inherit
        # the new ingestion default. Reading a review must not authorize a rebuild.
        if indexed_document and review_settings.get("chunk_layout") is None:
            review_settings["chunk_layout"] = (indexed_document.meta or {}).get(
                "chunk_layout"
            ) or "legacy"
        structural = (
            (indexed_document.meta or {}).get("structure_review") if indexed_document else None
        )
        if isinstance(structural, dict):
            review_settings.update(
                {key: structural.get(key, []) for key in ("accepted_edge_ids", "rejected_edge_ids")}
            )
        result: dict[str, Any] = {
            "id": job.id,
            "filename": job.filename,
            "source_hash": job.source_hash,
            "status": job.status,
            "page_count": job.page_count,
            "selected_pages": job.selected_pages,
            "total_pages": len(rows),
            "completed_pages": counts["completed"],
            "counts": dict(counts),
            "cached_pages": sum(row.cache_hit for row in rows if row.status == "completed"),
            "progress": counts["completed"] / len(rows) if rows else 0,
            "preview_available": job.status == "completed" and bool(job.preview_key),
            "ready_for_indexing": False,
            "indexing_performed": job.index_status == "indexed" and bool(job.indexed_document_id),
            "index_status": job.index_status,
            "index_revision": revision,
            "review_settings": {
                key: review_settings.get(key)
                for key in (
                    "excluded_block_ids",
                    "knowledge_points",
                    "source_type",
                    "review_note",
                    "accepted_edge_ids",
                    "rejected_edge_ids",
                    "chunk_layout",
                )
            },
            "index_present": indexed_document is not None,
            "index_error": job.index_error,
            "indexed_document_id": job.indexed_document_id,
            "approved_at": job.approved_at.isoformat() if job.approved_at else None,
            "partial_document": len(rows) != job.page_count,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
        if include_pages:
            result["pages"] = [
                {
                    "page_no": row.page_no,
                    "status": row.status,
                    "attempts": row.attempts,
                    "cache_hit": row.cache_hit,
                    "attempt_history": row.attempt_history,
                    "engine_version": row.engine_version,
                    "latency_seconds": row.latency_seconds,
                    "error_code": row.error_code,
                    "error_message": row.error_message,
                }
                for row in rows[(page - 1) * page_size : page * page_size]
            ]
        return result

    def list_jobs(self, user: User, page: int, page_size: int) -> dict[str, Any]:
        query = scope_query(self.db.query(OcrJob), OcrJob, user)
        total = query.count()
        jobs = (
            query.order_by(OcrJob.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"total": total, "items": [self.summary(job) for job in jobs]}

    def cancel(self, job_id: str, user: User) -> dict[str, Any]:
        job = require_scope(
            self.db.query(OcrJob).filter(OcrJob.id == job_id).with_for_update().first(), user
        )
        if job.status not in TERMINAL:
            job.status, job.cancel_requested_at = "cancelled", now()
            job.lease_token, job.lease_until = None, None  # fence out old workers immediately
            for checkpoint in self.db.query(OcrPage).filter(
                OcrPage.job_id == job_id, OcrPage.status != "completed"
            ):
                checkpoint.status = "cancelled"
                if (
                    checkpoint.attempt_history
                    and checkpoint.attempt_history[-1]["status"] == "running"
                ):
                    checkpoint.attempt_history = [
                        *checkpoint.attempt_history[:-1],
                        {
                            **checkpoint.attempt_history[-1],
                            "status": "cancelled",
                            "completed_at": now().isoformat(),
                        },
                    ]
            self.db.commit()
        return self.summary(job, include_pages=True)

    def resume(self, job_id: str, user: User) -> dict[str, Any]:
        job = require_scope(
            self.db.query(OcrJob).filter(OcrJob.id == job_id).with_for_update().first(), user
        )
        if job.status not in {"failed", "cancelled"}:
            raise InvalidKnowledgeError("仅失败或取消的OCR任务可以续跑")
        job.status, job.cancel_requested_at, job.dispatch_after = "pending", None, None
        job.retry_after = None
        job.lease_token, job.lease_until, job.error_code, job.error_message = None, None, None, None
        for checkpoint in self.db.query(OcrPage).filter(OcrPage.job_id == job_id):
            valid = checkpoint.status == "completed" and bool(
                checkpoint.result_key and checkpoint.result_hash
            )
            if valid and checkpoint.result_key and checkpoint.result_hash:
                try:
                    self.storage.read_json(checkpoint.result_key, checkpoint.result_hash)
                except (OSError, ValueError):
                    valid = False
            if not valid:
                checkpoint.status, checkpoint.attempts = "pending", 0
                checkpoint.error_code, checkpoint.error_message = None, None
                checkpoint.result_key, checkpoint.result_hash = None, None
        self.db.commit()
        enqueue(self.db, job_id, self.dispatch)
        return self.summary(job, include_pages=True)

    def preview_path(self, job_id: str, user: User) -> Path:
        job = self.get(job_id, user)
        if job.status != "completed" or not job.preview_key or not job.preview_hash:
            raise InvalidKnowledgeError("OCR 预览尚未完成")
        try:
            self.storage.read_json(job.preview_key, job.preview_hash)
        except (OSError, ValueError) as exc:
            raise InvalidKnowledgeError("OCR 预览文件缺失或校验失败") from exc
        return self.storage.path(job.preview_key)
