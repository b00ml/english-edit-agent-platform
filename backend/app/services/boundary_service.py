"""Explicit local 2/3-page review; parent OCR checkpoints and indexes are immutable."""

from __future__ import annotations

from typing import Any, Callable

from kombu.exceptions import OperationalError as BrokerError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.errors import InvalidKnowledgeError
from app.models import OcrBoundaryJob, OcrJob
from app.rag.ocr.storage import OcrStorage
from app.tenancy import require_scope


class WindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[StrictInt] = Field(min_length=2, max_length=3)
    local_compute_acknowledged: StrictBool


def send_window(identifier: str) -> None:
    from app.worker.celery_app import celery_app

    celery_app.send_task("app.worker.ocr_tasks.review_boundary", args=[identifier], queue="ocr")


class BoundaryService:
    def __init__(self, db: Session, dispatch: Callable[[str], None] = send_window) -> None:
        self.db, self.dispatch = db, dispatch

    def summary(self, row: OcrBoundaryJob) -> dict[str, Any]:
        self.db.refresh(row)
        return {
            key: getattr(row, key)
            for key in [
                "id",
                "job_id",
                "pages",
                "source_preview_hash",
                "status",
                "attempts",
                "cache_hit",
                "engine_version",
                "error",
            ]
        } | {
            "result_available": bool(row.result_key and row.status == "completed"),
            "indexing_performed": False,
            "embedding_calls": 0,
        }

    def create(self, job_id: str, body: WindowRequest, user: Any) -> dict[str, Any]:
        parent = require_scope(
            self.db.query(OcrJob).filter_by(id=job_id).with_for_update().first(), user
        )
        if (
            not body.local_compute_acknowledged
            or parent.status != "completed"
            or not parent.preview_hash
        ):
            raise InvalidKnowledgeError("需已完成原OCR并确认本地计算；不会自动入知识库")
        if body.pages != list(range(body.pages[0], body.pages[0] + len(body.pages))) or any(
            n not in parent.selected_pages for n in body.pages
        ):
            raise InvalidKnowledgeError("复核必须是原已完成范围内的连续2/3页，不能跨缺页")
        old = (
            self.db.query(OcrBoundaryJob)
            .filter_by(
                job_id=job_id, tenant_id=parent.tenant_id, source_preview_hash=parent.preview_hash
            )
            .filter(OcrBoundaryJob.status.in_(["pending", "running", "completed"]))
            .all()
        )
        for row in old:
            if row.pages == body.pages:
                return self.summary(row)
        row = OcrBoundaryJob(
            job_id=parent.id,
            tenant_id=parent.tenant_id,
            pages=body.pages,
            source_preview_hash=parent.preview_hash,
        )
        self.db.add(row)
        self.db.commit()
        try:
            self.dispatch(row.id)
        except (OSError, TimeoutError, BrokerError):
            pass  # Durable pending row remains for scheduler redelivery.
        return self.summary(row)

    def get(self, identifier: str, user: Any) -> OcrBoundaryJob:
        return require_scope(self.db.get(OcrBoundaryJob, identifier), user)

    def list(self, job_id: str, user: Any) -> dict[str, Any]:
        parent = require_scope(self.db.get(OcrJob, job_id), user)
        rows = (
            self.db.query(OcrBoundaryJob)
            .filter_by(job_id=job_id, tenant_id=parent.tenant_id)
            .order_by(OcrBoundaryJob.created_at.desc())
            .limit(50)
            .all()
        )
        return {"items": [self.summary(row) for row in rows]}

    def cancel(self, identifier: str, user: Any) -> dict[str, Any]:
        row = require_scope(
            self.db.query(OcrBoundaryJob).filter_by(id=identifier).with_for_update().first(), user
        )
        if row.status not in {"completed", "failed", "cancelled"}:
            row.status, row.token, row.lease_until = "cancelled", None, None
        self.db.commit()
        return self.summary(row)

    def resume(self, identifier: str, user: Any) -> dict[str, Any]:
        from app.config import settings

        row = require_scope(
            self.db.query(OcrBoundaryJob).filter_by(id=identifier).with_for_update().first(), user
        )
        parent = require_scope(self.db.get(OcrJob, row.job_id), user)
        if (
            row.status not in {"failed", "cancelled"}
            or row.attempts >= settings.RAG_OCR_BOUNDARY_MAX_ATTEMPTS
            or parent.preview_hash != row.source_preview_hash
        ):
            raise InvalidKnowledgeError("不可续跑或源已变化/次数耗尽，请核对后新建复核")
        row.status, row.error, row.token, row.lease_until = "pending", None, None, None
        self.db.commit()
        try:
            self.dispatch(row.id)
        except (OSError, TimeoutError, BrokerError):
            pass
        return self.summary(row)

    def result(self, identifier: str, user: Any) -> dict[str, Any]:
        row = self.get(identifier, user)
        parent = require_scope(self.db.get(OcrJob, row.job_id), user)
        if (
            row.status != "completed"
            or not row.result_key
            or parent.preview_hash != row.source_preview_hash
        ):
            raise InvalidKnowledgeError("复核未完成或原预览版本已变化")
        try:
            value = OcrStorage().read_json(row.result_key, row.result_hash)
        except (OSError, ValueError) as exc:
            raise InvalidKnowledgeError("复核快照缺失或校验失败；原资料未修改") from exc
        if not isinstance(value, dict):
            raise InvalidKnowledgeError("复核结果不是对象")
        return dict(value)
