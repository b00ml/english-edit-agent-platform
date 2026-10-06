"""One explicit local multi-page call, durable fencing/cache and no index mutation."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from billiard.exceptions import SoftTimeLimitExceeded
from kombu.exceptions import OperationalError as BrokerError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError, PdfStreamError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.errors import DocumentParseError
from app.models import OcrBoundaryJob, OcrJob
from app.rag.ocr.adapter import page_blocks
from app.rag.ocr.mineru import (
    MinerUClient,
    NativeDocument,
    OcrParseError,
    validate_window,
)
from app.rag.ocr.storage import OcrStorage
from app.rag.parser import _finish
from app.rag.structure import build_structure
from app.services.boundary_service import send_window
from app.services.ocr_service import now
from app.versioning import hash_value


def run_boundary(
    identifier: str,
    factory: Callable[[], Session] = SessionLocal,
    client_factory: Any = MinerUClient,
) -> dict[str, Any]:
    token = str(uuid.uuid4())
    storage = OcrStorage()
    with factory() as db:
        changed = (
            db.query(OcrBoundaryJob)
            .filter_by(id=identifier, status="pending")
            .filter(OcrBoundaryJob.attempts < settings.RAG_OCR_BOUNDARY_MAX_ATTEMPTS)
            .update(
                {
                    "status": "running",
                    "token": token,
                    "lease_until": now()
                    + timedelta(seconds=settings.RAG_OCR_BOUNDARY_TIMEOUT + 90),
                    "attempts": OcrBoundaryJob.attempts + 1,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if not changed:
            return {"status": "not_claimed"}
        row = db.get(OcrBoundaryJob, identifier)
        if row is None:
            return {"status": "stale"}
        parent = db.get(OcrJob, row.job_id)
        if parent is None:
            return {"status": "stale"}
        parent_id = parent.id
        pages = list(row.pages)
        tenant = row.tenant_id
        preview_hash = row.source_preview_hash
        source_hash = parent.source_hash
        input_key = parent.input_key
        filename = parent.filename
        if parent.status != "completed" or parent.preview_hash != preview_hash:
            row.status, row.token, row.error = "failed", None, "原预览变化，未调用OCR"
            db.commit()
            return {"status": "failed"}

    def cancelled() -> bool:
        with factory() as db:
            current = db.get(OcrBoundaryJob, identifier)
            return current is None or current.token != token or current.status != "running"

    try:
        client = client_factory(timeout=settings.RAG_OCR_BOUNDARY_TIMEOUT, cancel_check=cancelled)
        version = client.server_version()
        code_root = Path(__file__).parents[1]
        code_hash = hash_value(
            [
                Path(__file__).read_text(encoding="utf-8"),
                *[
                    code_root.joinpath(path).read_text(encoding="utf-8")
                    for path in (
                        "rag/ocr/adapter.py",
                        "rag/ocr/mineru.py",
                        "rag/tables.py",
                        "rag/structure/builder.py",
                        "rag/structure/policy.yml",
                        "rag/structure/tables.py",
                        "rag/structure/references.py",
                    )
                ],
                settings.RAG_STRUCTURE_MAX_BLOCKS,
                settings.RAG_OCR_URL,
            ]
        )
        cache = (
            "boundary-cache/"
            + hash_value(
                [
                    tenant,
                    source_hash,
                    preview_hash,
                    pages,
                    version,
                    code_hash,
                    settings.RAG_OCR_CACHE_REVISION,
                ]
            )
            + ".json"
        )
        hit = False
        if storage.path(cache).exists():
            try:
                envelope = storage.read_json(cache)
                if envelope["hash"] != hash_value(envelope["payload"]):
                    raise ValueError("cache hash mismatch")
                result = envelope["payload"]
                hit = True
            except (OSError, ValueError, KeyError):
                hit = False
        if cancelled():
            return {"status": "stale"}
        if not hit:
            raw = storage.input_bytes(input_key, source_hash)
            reader = PdfReader(io.BytesIO(raw))
            writer = PdfWriter()
            for number in pages:
                writer.add_page(reader.pages[number - 1])
            output = io.BytesIO()
            writer.write(output)
            native = client.parse_window(output.getvalue(), len(pages), filename="boundary.pdf")
            native = validate_window(native.model_dump(by_alias=True), len(pages))
            blocks = []
            warnings = []
            for physical, local in zip(pages, native.pages):
                one = NativeDocument.model_validate(
                    {
                        "schema": "docvortex.middle",
                        "schema_version": "2.0",
                        "metadata": native.metadata.model_dump(),
                        "pages": [
                            {"page_idx": 0, "blocks": [b.model_dump() for b in local.blocks]}
                        ],
                    }
                )
                converted, messages = page_blocks(one, physical)
                blocks.extend(converted)
                warnings.extend(messages)
            document = _finish(
                filename,
                blocks,
                warnings,
                stats={
                    "selected_pages": pages,
                    "partial_document": True,
                    "boundary_review_only": True,
                },
            )
            document["source_hash"] = source_hash
            document["parser_version"] = "boundary-middle2-v1"
            structure = build_structure(document, max_blocks=settings.RAG_STRUCTURE_MAX_BLOCKS)
            result = {
                "version": "boundary-review-v1",
                "source_job_id": parent_id,
                "source_preview_hash": preview_hash,
                "source_hash": source_hash,
                "global_pages": pages,
                "local_to_global": {str(i): n for i, n in enumerate(pages)},
                "engine_version": version,
                "native": native.model_dump(by_alias=True),
                "document": document,
                "structure": structure,
                "source_applied": False,
                "embedding_calls": 0,
                "notice": "独立复核快照，不替换原逐页检查点/活动索引；差异需人工核对及显式重新解析/重建",
            }
            storage.write_json(cache, {"payload": result, "hash": hash_value(result)})
        if (
            not isinstance(result, dict)
            or result.get("global_pages") != pages
            or result.get("source_hash") != source_hash
            or result.get("source_preview_hash") != preview_hash
        ):
            raise ValueError("Cached boundary identity mismatch")
        result_key = f"boundary-jobs/{identifier}/{token}.json"
        result_hash = storage.write_json(result_key, result)
        with factory() as db:
            current = db.get(OcrBoundaryJob, identifier)
            if current is None:
                return {"status": "stale"}
            source = db.get(OcrJob, current.job_id)
            if source is None:
                return {"status": "stale"}
            if cancelled():
                return {"status": "stale"}
            if source.preview_hash != preview_hash:
                db.query(OcrBoundaryJob).filter_by(
                    id=identifier, token=token, status="running"
                ).update(
                    {
                        "status": "failed",
                        "token": None,
                        "lease_until": None,
                        "error": "原预览变化，复核未应用",
                    },
                    synchronize_session=False,
                )
                db.commit()
                return {"status": "stale"}
            changed = (
                db.query(OcrBoundaryJob)
                .filter_by(id=identifier, token=token, status="running")
                .update(
                    {
                        "status": "completed",
                        "result_key": result_key,
                        "result_hash": result_hash,
                        "engine_version": version,
                        "cache_hit": hit,
                        "token": None,
                        "lease_until": None,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return {"status": "completed" if changed else "stale", "cache_hit": hit}
    except (
        DocumentParseError,
        OcrParseError,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        PdfReadError,
        PdfStreamError,
        SoftTimeLimitExceeded,
        SQLAlchemyError,
    ) as exc:
        with factory() as db:
            db.query(OcrBoundaryJob).filter_by(id=identifier, token=token, status="running").update(
                {
                    "status": "failed",
                    "token": None,
                    "lease_until": None,
                    "error": "本地多页复核失败：" + type(exc).__name__,
                },
                synchronize_session=False,
            )
            db.commit()
        return {"status": "failed", "error_type": type(exc).__name__}


def sweep_boundaries(
    factory: Callable[[], Session] = SessionLocal, dispatch: Callable[[str], None] = send_window
) -> dict[str, int]:
    with factory() as db:
        expired = (
            db.query(OcrBoundaryJob)
            .filter(OcrBoundaryJob.status == "running", OcrBoundaryJob.lease_until <= now())
            .limit(50)
            .all()
        )
        for row in expired:
            row.status = (
                "pending" if row.attempts < settings.RAG_OCR_BOUNDARY_MAX_ATTEMPTS else "failed"
            )
            row.token, row.lease_until, row.error = None, None, "复核租约过期，原成功页未修改"
        db.query(OcrBoundaryJob).filter(
            OcrBoundaryJob.status == "pending",
            OcrBoundaryJob.attempts >= settings.RAG_OCR_BOUNDARY_MAX_ATTEMPTS,
        ).update({"status": "failed", "error": "复核尝试次数耗尽"}, synchronize_session=False)
        db.commit()
        pending = db.query(OcrBoundaryJob).filter_by(status="pending").limit(50).all()
        sent = 0
        for row in pending:
            try:
                dispatch(row.id)
                sent += 1
            except (OSError, TimeoutError, BrokerError):
                pass
        return {"boundary_recovered": len(expired), "boundary_sent": sent}
