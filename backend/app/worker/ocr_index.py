"""Explicitly approved real embedding; uncertain paid attempts never auto-retry."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Callable

from billiard.exceptions import SoftTimeLimitExceeded
from openai import APIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.errors import PlatformError
from app.models import KnowledgeDocument, OcrJob
from app.rag.indexer import index_document
from app.services.ocr_review import ReviewApproval, plan
from app.services.ocr_service import now


def run_index(job_id: str, factory: Callable[[], Session] = SessionLocal) -> dict[str, Any]:
    db = factory()
    token = str(uuid.uuid4())
    try:
        changed = (
            db.query(OcrJob)
            .filter(
                OcrJob.id == job_id, OcrJob.status == "completed", OcrJob.index_status == "pending"
            )
            .update(
                {
                    "index_status": "indexing",
                    "index_token": token,
                    "index_until": now() + timedelta(seconds=settings.RAG_OCR_INDEX_TIMEOUT + 120),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if not changed:
            return {"status": "not_claimed", "job_id": job_id}
        job = db.query(OcrJob).filter(OcrJob.id == job_id).with_for_update().one()
        if job.index_token != token:
            db.rollback()
            return {"status": "stale", "job_id": job_id}
        approval = ReviewApproval.model_validate(job.approval)
        if not all(
            (
                approval.source_reviewed,
                approval.warnings_acknowledged,
                approval.paid_embedding_acknowledged,
            )
        ):
            raise ValueError("缺少有效审核/费用授权")
        actor = type(
            "IndexActor",
            (),
            {"id": job.approved_by, "role": "researcher", "tenant_id": job.tenant_id},
        )()
        current = plan(
            db,
            job_id,
            actor,
            approval.excluded_block_ids,
            approval.accepted_edge_ids,
            approval.rejected_edge_ids,
            approval.chunk_layout,
        )
        if (
            approval.preview_hash != current["preview_hash"]
            or approval.plan_hash != current["plan_hash"]
        ):
            raise ValueError("审核后预览/切块配置变化，未调用embedding")
        if not current["chunks"] or len(current["chunks"]) > settings.RAG_OCR_INDEX_MAX_CHUNKS:
            raise ValueError("审核后的分块数超出当前索引上限，未调用embedding")
        partial = bool(current["document"]["stats"].get("partial_document"))
        if partial and not approval.accept_selected_pages:
            raise ValueError("所选页索引缺少授权")
        document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "english-edit:ocr:" + job_id))
        existing = db.get(KnowledgeDocument, document_id)
        if existing is not None and (
            existing.tenant_id != job.tenant_id or (existing.meta or {}).get("ocr_job_id") != job_id
        ):
            raise ValueError("索引文档归属不一致")
        revision = (
            int((existing.meta or {}).get("index_revision", 1)) if existing is not None else 0
        )
        if approval.rebuild_index and approval.expected_index_revision != revision:
            raise ValueError("领取任务后索引版本变化，未调用embedding")
        if existing is None or approval.rebuild_index:
            details: dict[str, Any] = {}
            document = current["document"]
            document["stats"]["reviewed_index_scope"] = (
                "selected_pages" if partial else "full_document"
            )
            document["stats"]["reviewed_ocr_job_id"] = job_id
            index_document(
                db,
                source_type=approval.source_type,
                source_name=document["source_name"][:128],
                text=document,
                knowledge_points=approval.knowledge_points,
                tenant_id=job.tenant_id,
                meta={
                    "ocr_job_id": job_id,
                    "index_revision": revision + 1,
                    "index_plan_hash": approval.plan_hash,
                    "approval_preview_hash": approval.preview_hash,
                    "source_verified": True,
                    "review_note": approval.review_note,
                    "index_scope": "selected_pages" if partial else "full_document",
                },
                details=details,
                document_id=document_id,
                commit=False,
                replace_existing=existing is not None,
                chunk_layout=approval.chunk_layout,
                structure_decisions={
                    "accepted_edge_ids": approval.accepted_edge_ids,
                    "rejected_edge_ids": approval.rejected_edge_ids,
                },
            )
        elif (
            existing.tenant_id != job.tenant_id or (existing.meta or {}).get("ocr_job_id") != job_id
        ):
            raise ValueError("索引文档归属不一致")
        job.index_status, job.indexed_document_id = "indexed", document_id
        job.index_token, job.index_until, job.index_error = None, None, None
        db.commit()  # knowledge rows and OCR indexed state commit atomically
        return {"status": "indexed", "document_id": document_id, "job_id": job_id}
    except (
        PlatformError,
        ValueError,
        OSError,
        SoftTimeLimitExceeded,
        APIError,
        SQLAlchemyError,
    ) as exc:
        db.rollback()
        message = "索引未完成；供应商可能已计费，请核对Trace后显式重试"
        if isinstance(exc, ValueError):
            message = str(exc)[:500]
        db.query(OcrJob).filter(OcrJob.id == job_id, OcrJob.index_token == token).update(
            {
                "index_status": "needs_attention",
                "index_error": message,
                "index_token": None,
                "index_until": None,
            },
            synchronize_session=False,
        )
        db.commit()
        return {"status": "needs_attention", "job_id": job_id}
    finally:
        db.close()
