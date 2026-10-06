"""Human-confirmed immutable OCR indexing plan; no paid call in review/approval HTTP requests."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import InvalidKnowledgeError
from app.models import KnowledgeDocument, OcrJob, User
from app.rag.document import finalize_document
from app.rag.indexer import configured_chunking
from app.rag.knowledge_points import load_catalog
from app.rag.preview import preview_document
from app.services.ocr_service import OcrService, now
from app.tenancy import require_scope


class ReviewApproval(BaseModel):
    preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_reviewed: StrictBool
    warnings_acknowledged: StrictBool
    paid_embedding_acknowledged: StrictBool
    accept_selected_pages: StrictBool = False
    retry_authorized: StrictBool = False
    chunk_layout: Literal["legacy", "structure"] | None = None
    rebuild_index: StrictBool = False
    expected_index_revision: int = Field(default=0, ge=0)
    source_type: Literal["教材", "课标", "真题", "练习册", "其他"] = "教材"
    knowledge_points: list[str] = Field(default_factory=list, max_length=32)
    review_note: str = Field(default="", max_length=1000)
    excluded_block_ids: list[str] = Field(default_factory=list, max_length=5000)
    accepted_edge_ids: list[str] = Field(default_factory=list, max_length=128)
    rejected_edge_ids: list[str] = Field(default_factory=list, max_length=128)


def send_index(job_id: str) -> None:
    from app.worker.celery_app import celery_app

    celery_app.send_task("app.worker.ocr_tasks.index_ocr_job", args=[job_id], queue="ocr")


def plan(
    db: Session,
    job_id: str,
    user: User,
    excluded_block_ids: list[str] | None = None,
    accepted_edge_ids: list[str] | None = None,
    rejected_edge_ids: list[str] | None = None,
    chunk_layout: str | None = None,
) -> dict[str, Any]:
    service = OcrService(db)
    job = service.get(job_id, user)
    original = (
        service.storage.read_json(str(job.preview_key), job.preview_hash)
        if job.status == "completed" and job.preview_key and job.preview_hash
        else None
    )
    if original is None:
        raise InvalidKnowledgeError("OCR 尚未完成，不能审核或索引")
    document = copy.deepcopy(original["document"])
    excluded = sorted(set(excluded_block_ids or []))
    known = {block["block_id"] for block in document["blocks"]}
    if set(excluded) - known:
        raise InvalidKnowledgeError("排除块不属于当前原文预览")
    document["blocks"] = [
        block
        for block in document["blocks"]
        if block["block_id"] not in excluded or block["block_type"] == "page_break"
    ]
    document["stats"]["excluded_block_ids"] = excluded
    if excluded:
        document["warnings"].append("人工排除部分识别块；原页/原生快照保留，排除内容不会embedding")
    finalize_document(document)
    result = preview_document(document, accepted_edge_ids, rejected_edge_ids, chunk_layout)
    signature = {
        "preview_hash": job.preview_hash,
        "excluded_block_ids": excluded,
        "chunk_config": asdict(configured_chunking(chunk_layout)),
        "parent": [
            settings.RAG_PARENT_CHILD_ENABLED,
            settings.RAG_PARENT_SIZE,
            settings.RAG_PARENT_MIN_CHARS,
        ],
        "embedding": [settings.EMBEDDING_MODEL_NAME, settings.EMBEDDING_DIM],
        "inputs": [
            hashlib.sha256(chunk["embedding_content"].encode()).hexdigest()
            for chunk in result["chunks"]
        ],
        "catalog_hash": load_catalog().hash,
        "structure_signature": (result["structure"] or {}).get("signature"),
        "index_input_signature": result["diagnostics"].get("plan_signature"),
    }
    result["preview_hash"] = job.preview_hash
    result["plan_hash"] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    result["embedding"] = {
        "model": settings.EMBEDDING_MODEL_NAME,
        "dimension": settings.EMBEDDING_DIM,
        "text_count": len(result["chunks"]),
        "characters": sum(len(chunk["embedding_content"]) for chunk in result["chunks"]),
        "price_configured": settings.EMBEDDING_MODEL_NAME in settings.MODEL_PRICES,
        "provider_bill_verified": False,
    }
    return result


def approve(db: Session, job_id: str, body: ReviewApproval, user: User) -> dict[str, Any]:
    job = require_scope(
        db.query(OcrJob).filter(OcrJob.id == job_id).with_for_update().first(), user
    )
    if (
        job.index_status in {"pending", "indexing"}
        or job.index_status == "indexed"
        and not body.rebuild_index
    ):
        return OcrService(db).summary(job)  # double-click/replay is idempotent, never re-pays
    document = (
        db.get(KnowledgeDocument, job.indexed_document_id) if job.indexed_document_id else None
    )
    revision = int((document.meta or {}).get("index_revision", 1)) if document else 0
    if document is not None and not body.rebuild_index:
        raise InvalidKnowledgeError("当前索引已修改，需要显式授权重建")
    if body.rebuild_index and body.expected_index_revision != revision:
        raise InvalidKnowledgeError("索引版本已变化，请刷新后重建，避免重复付费")
    if job.index_status in {"failed", "needs_attention"} and not body.retry_authorized:
        raise InvalidKnowledgeError("上次索引失败/状态不确定，重试可能再次计费，请显式确认")
    if not (
        body.source_reviewed and body.warnings_acknowledged and body.paid_embedding_acknowledged
    ):
        raise InvalidKnowledgeError("必须确认原页、解析告警以及真实embedding调用费用")
    current = plan(
        db,
        job_id,
        user,
        body.excluded_block_ids,
        body.accepted_edge_ids,
        body.rejected_edge_ids,
        body.chunk_layout,
    )
    if current["preview_hash"] != body.preview_hash or current["plan_hash"] != body.plan_hash:
        raise InvalidKnowledgeError("预览/切块配置已变化，请重新打开审核")
    if current["document"]["stats"].get("partial_document") and not body.accept_selected_pages:
        raise InvalidKnowledgeError("仅解析所选页，必须单独确认仅索引这些页，不能冒充全书")
    if not current["chunks"] or len(current["chunks"]) > settings.RAG_OCR_INDEX_MAX_CHUNKS:
        raise InvalidKnowledgeError("没有可索引内容或分块超过本次索引上限")
    if any(not point.strip() or len(point) > 128 for point in body.knowledge_points):
        raise InvalidKnowledgeError("知识点标签为空或超出长度")
    load_catalog().tags(body.knowledge_points)
    job.approval = body.model_dump()
    job.approved_by, job.approved_at = user.id, now()
    job.index_status, job.index_error, job.index_dispatch_after = "pending", None, None
    db.commit()
    dispatch_index(db, job_id)
    return OcrService(db).summary(job)


def dispatch_index(db: Session, job_id: str) -> bool:
    from kombu.exceptions import OperationalError as BrokerError
    from sqlalchemy import or_

    changed = (
        db.query(OcrJob)
        .filter(
            OcrJob.id == job_id,
            OcrJob.index_status == "pending",
            or_(OcrJob.index_dispatch_after.is_(None), OcrJob.index_dispatch_after <= now()),
        )
        .update(
            {"index_dispatch_after": now() + timedelta(seconds=settings.RAG_OCR_SWEEP_SECONDS * 2)},
            synchronize_session=False,
        )
    )
    db.commit()
    if not changed:
        return False
    try:
        send_index(job_id)
    except (BrokerError, OSError, TimeoutError):
        return False  # durable pending state is redelivered by the existing OCR scheduler
    return True
