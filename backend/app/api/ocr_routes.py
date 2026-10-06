"""Background OCR APIs; authorization/tenant checks in code, never paid indexing."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, StrictInt, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import InvalidKnowledgeError
from app.models import User
from app.security import require_permission
from app.services.boundary_service import BoundaryService, WindowRequest
from app.services.ocr_review import ReviewApproval, approve, plan
from app.services.ocr_service import OcrService

router = APIRouter(prefix="/api/knowledge/ocr/jobs", tags=["OCR"])


class LocalImport(BaseModel):
    files: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        min_length=1, max_length=50
    )
    pages: list[StrictInt] | None = None


@router.post("/upload", status_code=202)
def upload(
    file: UploadFile = File(...),
    pages: str = Form("null"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    try:
        selected: list[int] | None = TypeAdapter(list[StrictInt] | None).validate_python(
            json.loads(pages)
        )
    except (ValueError, ValidationError) as exc:
        raise InvalidKnowledgeError("pages必须是整数列表或null") from exc
    return OcrService(db).create(file.file, file.filename or "", user, selected)


@router.post("/import-local", status_code=202)
def import_local(
    body: LocalImport,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    return OcrService(db).import_local(body.files, user, body.pages)


@router.get("")
def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    return OcrService(db).list_jobs(user, page, page_size)


@router.get("/{job_id}")
def job_detail(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    service = OcrService(db)
    return service.summary(
        service.get(job_id, user), include_pages=True, page=page, page_size=page_size
    )


@router.post("/{job_id}/cancel")
def cancel(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    return OcrService(db).cancel(job_id, user)


@router.post("/{job_id}/resume", status_code=202)
def resume(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    return OcrService(db).resume(job_id, user)


@router.get("/{job_id}/preview")
def preview(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("ops:read"))
) -> FileResponse:
    path = OcrService(db).preview_path(job_id, user)
    return FileResponse(path, media_type="application/json", filename=f"ocr-preview-{job_id}.json")


@router.get("/{job_id}/review")
def review(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("ops:read"))
) -> dict[str, Any]:
    return plan(db, job_id, user)


@router.post("/{job_id}/approve-index", status_code=202)
def approve_index(
    job_id: str,
    body: ReviewApproval,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    return approve(db, job_id, body, user)


@router.get("/{job_id}/source-pages/{page_no}")
def source_page(
    job_id: str,
    page_no: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> Response:
    import json

    from app.errors import DocumentParseError
    from app.rag.ocr.render import render_page

    service = OcrService(db)
    job = service.get(job_id, user)
    if page_no not in job.selected_pages:
        raise InvalidKnowledgeError("该页不在本任务选择范围")
    try:
        service.storage.input_bytes(job.input_key, job.source_hash)
        png, dimensions = render_page(service.storage.path(job.input_key), page_no)
    except (OSError, ValueError, DocumentParseError) as exc:
        raise InvalidKnowledgeError("原页无法安全渲染，请核对文件") from exc
    match = "unknown"
    if job.preview_key and job.preview_hash:
        document = service.storage.read_json(job.preview_key, job.preview_hash)["document"]
        for snapshot in json.loads(document["original_text"] or "[]"):
            if snapshot["source_page_no"] == page_no:
                geometry = (
                    snapshot["native_result"]
                    .get("extensions", {})
                    .get("docvortex_layout", {})
                    .get("pages", [])
                )
                if geometry and all(key in geometry[0] for key in ("width_pt", "height_pt")):
                    match = (
                        "true"
                        if max(
                            abs(geometry[0]["width_pt"] - dimensions[0]),
                            abs(geometry[0]["height_pt"] - dimensions[1]),
                        )
                        < 1
                        else "mismatch"
                    )
    return Response(
        png,
        media_type="image/png",
        headers={"X-OCR-Coordinate-Match": match, "Cache-Control": "no-store"},
    )


class ReviewSelection(BaseModel):
    chunk_layout: Literal["legacy", "structure"] | None = None
    excluded_block_ids: list[str] = Field(default_factory=list, max_length=5000)
    accepted_edge_ids: list[str] = Field(default_factory=list, max_length=128)
    rejected_edge_ids: list[str] = Field(default_factory=list, max_length=128)


@router.post("/{job_id}/review-plan")
def review_plan(
    job_id: str,
    body: ReviewSelection,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    return plan(
        db,
        job_id,
        user,
        body.excluded_block_ids,
        body.accepted_edge_ids,
        body.rejected_edge_ids,
        body.chunk_layout,
    )


@router.post("/{job_id}/boundary-reviews", status_code=202)
def create_boundary(
    job_id: str,
    body: WindowRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    return BoundaryService(db).create(job_id, body, user)


@router.get("/{job_id}/boundary-reviews")
def list_boundary(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(require_permission("ops:read"))
) -> dict[str, Any]:
    return BoundaryService(db).list(job_id, user)


@router.get("/{job_id}/boundary-reviews/{identifier}")
def get_boundary(
    job_id: str,
    identifier: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    service = BoundaryService(db)
    row = service.get(identifier, user)
    if row.job_id != job_id:
        raise InvalidKnowledgeError("复核不属于该任务")
    return service.summary(row)


@router.get("/{job_id}/boundary-reviews/{identifier}/result")
def boundary_result(
    job_id: str,
    identifier: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    service = BoundaryService(db)
    row = service.get(identifier, user)
    if row.job_id != job_id:
        raise InvalidKnowledgeError("复核不属于该任务")
    return service.result(identifier, user)


@router.post("/{job_id}/boundary-reviews/{identifier}/{action}")
def control_boundary(
    job_id: str,
    identifier: str,
    action: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    service = BoundaryService(db)
    row = service.get(identifier, user)
    if row.job_id != job_id or action not in {"cancel", "resume"}:
        raise InvalidKnowledgeError("无效复核控制")
    return (
        service.cancel(identifier, user) if action == "cancel" else service.resume(identifier, user)
    )
