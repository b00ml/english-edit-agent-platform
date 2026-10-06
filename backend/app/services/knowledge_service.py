"""Knowledge operations share one indexing/retrieval path and explicit tenant scope."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.errors import (
    ChunkingError,
    InvalidKnowledgeError,
    KnowledgeNoIndexableTextError,
    KnowledgeUploadTooLargeError,
)
from app.models import KnowledgeChunk, KnowledgeDocument, OcrJob, User
from app.rag.document import ParsedDocument
from app.rag.indexer import index_document
from app.rag.knowledge_points import ScopeMode
from app.rag.parser import DocumentParseError, UnsupportedFileTypeError, parse_document, parse_text
from app.rag.preview import preview_document
from app.rag.retriever import retrieve
from app.rag.structure.runtime import active_rows, derive
from app.tenancy import require_scope, scope_query


class UploadResult(TypedDict):
    chunks: int
    source_type: str
    source_name: str
    knowledge_point: str | None
    warnings: list[str]
    diagnostics: dict[str, Any]


class KnowledgeList(TypedDict):
    total: int
    items: list[KnowledgeChunk]


class StructureReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_index_revision: int = Field(ge=1)
    source_reviewed: StrictBool
    accepted_edge_ids: list[str] = Field(default_factory=list, max_length=128)
    rejected_edge_ids: list[str] = Field(default_factory=list, max_length=128)


class RetrievalResult(TypedDict):
    query: str
    snippets: list[str]
    citations: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    protocol_version: str
    bundles: list[dict[str, Any]]


class KnowledgeService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upload_text(
        self,
        text: str | ParsedDocument,
        source_type: str,
        source_name: str,
        knowledge_point: str | None,
        meta: dict[str, Any] | None,
        user: User,
        knowledge_points: list[str] | None = None,
        chunk_layout: str | None = None,
    ) -> UploadResult:
        if (
            not source_type.strip()
            or len(source_type) > 32
            or not source_name.strip()
            or len(source_name) > 128
            or (knowledge_point is not None and len(knowledge_point) > 128)
        ):
            raise InvalidKnowledgeError("资料类型/名称/知识点不符合长度或非空要求")
        if isinstance(text, str) and len(text.encode("utf-8")) > 10 * 1024 * 1024:
            raise KnowledgeUploadTooLargeError("资料内容过大（上限 10MB）")
        has_content = bool(text.get("blocks")) if isinstance(text, dict) else bool(text.strip())
        if not has_content:
            raise InvalidKnowledgeError("资料内容不能为空")
        document = text if isinstance(text, dict) else parse_text(f"{source_name}.md", text)
        if document["stats"].get("partial_document"):
            raise InvalidKnowledgeError("PDF 仅解析了部分页，当前禁止部分结果正式入库")
        if not document["text"].strip():
            raise KnowledgeNoIndexableTextError(
                "文件中没有可索引文字；扫描 PDF/图片资料需要先进行 OCR；当前上传入口不自动执行 OCR。"
            )
        details: dict[str, Any] = {}
        try:
            chunks = index_document(
                self.db,
                source_type=source_type,
                source_name=source_name,
                text=document,
                knowledge_point=knowledge_point,
                meta=meta,
                tenant_id=user.tenant_id,
                details=details,
                knowledge_points=knowledge_points,
                chunk_layout=chunk_layout,
            )
        except (ChunkingError, ValueError) as exc:
            raise InvalidKnowledgeError(str(exc)) from exc
        if chunks == 0:
            raise KnowledgeNoIndexableTextError(
                "没有可索引文字，未调用 embedding；请检查原文或先进行 OCR"
            )
        return {
            "chunks": chunks,
            "source_type": source_type,
            "source_name": source_name,
            "knowledge_point": details.get("canonical_knowledge_point", knowledge_point),
            "warnings": document["warnings"],
            "diagnostics": details["diagnostics"],
        }

    def preview_file(
        self, filename: str, content: bytes, chunk_layout: str | None = None
    ) -> dict[str, Any]:
        """Parse and chunk without embedding, database writes or paid calls."""
        document = self._parse_file(filename, content)
        return preview_document(document, chunk_layout=chunk_layout)

    def _parse_file(self, filename: str, content: bytes) -> ParsedDocument:
        if not filename or not content:
            raise InvalidKnowledgeError("文件名与文件内容不能为空")
        if len(content) > 10 * 1024 * 1024:
            raise KnowledgeUploadTooLargeError("文件过大（上限 10MB）")
        try:
            return parse_document(filename, content)
        except (UnsupportedFileTypeError, DocumentParseError, ValueError) as exc:
            raise InvalidKnowledgeError("文件不支持或无法解析") from exc

    def upload_file(
        self,
        filename: str,
        content: bytes,
        source_type: str,
        knowledge_point: str | None,
        user: User,
        knowledge_points: list[str] | None = None,
        chunk_layout: str | None = None,
    ) -> UploadResult:
        document = self._parse_file(filename, content)
        return self.upload_text(
            document,
            source_type,
            Path(filename).stem,
            knowledge_point,
            {"filename": filename},
            user,
            knowledge_points,
            chunk_layout,
        )

    def list_knowledge(
        self, source_type: str | None, page: int, page_size: int, user: User
    ) -> KnowledgeList:
        query = scope_query(self.db.query(KnowledgeChunk), KnowledgeChunk, user).filter(
            or_(KnowledgeChunk.chunk_type.is_(None), KnowledgeChunk.chunk_type != "parent")
        )
        if source_type:
            query = query.filter(KnowledgeChunk.source_type == source_type)
        total = query.count()
        rows = (
            query.order_by(KnowledgeChunk.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"total": total, "items": rows}

    def chunk_diagnostics(self, chunk_id: str, user: User) -> dict[str, Any]:
        row = require_scope(self.db.get(KnowledgeChunk, chunk_id), user)
        document = None
        if row.document_id:
            document = require_scope(self.db.get(KnowledgeDocument, row.document_id), user)
        return {
            "chunk_id": row.id,
            "legacy": row.document_id is None,
            "meta": row.meta or {},
            "document": (
                {
                    "id": document.id,
                    "parser_version": document.parser_version,
                    "chunk_layout": (document.meta or {}).get("chunk_layout", "legacy"),
                    "index_input_signature": (document.meta or {}).get("index_input_signature"),
                    "warnings": document.warnings,
                    "stats": document.stats,
                    "blocks": document.blocks,
                    "normalized_text": document.normalized_text,
                    "original_text": document.original_text,
                }
                if document
                else None
            ),
        }

    def _lock_index_owner(self, document_id: str, user: User) -> OcrJob | None:
        owner = (
            self.db.query(OcrJob)
            .filter(OcrJob.indexed_document_id == document_id)
            .with_for_update()
            .first()
        )
        if owner is not None:
            require_scope(owner, user)
            if owner.index_status in {"pending", "indexing"}:
                raise InvalidKnowledgeError("索引重建正在执行，不能同时删除")
        return owner

    def list_documents(self, page: int, page_size: int, user: User) -> dict[str, Any]:
        query = scope_query(self.db.query(KnowledgeDocument), KnowledgeDocument, user)
        documents = (
            query.order_by(KnowledgeDocument.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = []
        for document in documents:
            rows = (
                self.db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.document_id == document.id)
                .all()
            )
            items.append(
                {
                    "id": document.id,
                    "source_name": document.source_name,
                    "source_type": document.source_type,
                    "status": document.status,
                    "leaf_count": sum(row.chunk_type != "parent" for row in rows),
                    "parent_count": sum(row.chunk_type == "parent" for row in rows),
                    "index_revision": int((document.meta or {}).get("index_revision", 1)),
                    "ocr_job_id": (document.meta or {}).get("ocr_job_id"),
                    "parser_version": document.parser_version,
                    "source_hash": document.source_hash,
                    "chunk_layout": (document.meta or {}).get("chunk_layout", "legacy"),
                    "index_input_signature": (document.meta or {}).get("index_input_signature"),
                    "embedding_models": sorted(
                        {row.embedding_model for row in rows if row.embedding_model}
                    ),
                    "created_at": document.created_at.isoformat(),
                }
            )
        return {"total": query.count(), "items": items}

    def delete_document(self, document_id: str, user: User) -> dict[str, Any]:
        owner = self._lock_index_owner(document_id, user)
        document = require_scope(
            self.db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.id == document_id)
            .with_for_update()
            .first(),
            user,
        )
        if owner is not None:
            owner.indexed_document_id, owner.index_status, owner.index_error = None, "removed", None
        chunks = self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id)
        # Count before DELETE: self-referential FK cascades can hide children from rowcount.
        count = chunks.count()
        chunks.delete(synchronize_session=False)
        self.db.delete(document)
        self.db.commit()
        return {
            "deleted_document": document_id,
            "deleted_chunks": count,
            "ocr_source_retained": owner is not None,
        }

    def delete_knowledge(self, chunk_id: str, user: User) -> dict[str, str]:
        row = require_scope(self.db.get(KnowledgeChunk, chunk_id), user)
        if row.chunk_type == "parent":
            raise InvalidKnowledgeError("不能直接删除父块；请按资料撤除索引或删除叶子块")
        document_id = row.document_id
        owner = self._lock_index_owner(document_id, user) if document_id else None
        parent_id, previous_id, next_id = row.parent_chunk_id, row.prev_chunk_id, row.next_chunk_id
        if parent_id:
            # A surviving parent must not re-inject deleted text into retrieval context.
            parent = require_scope(self.db.get(KnowledgeChunk, parent_id), user)
            self.db.query(KnowledgeChunk).filter(
                KnowledgeChunk.parent_chunk_id == parent_id
            ).update({"parent_chunk_id": None, "chunk_type": "single"}, synchronize_session=False)
            self.db.flush()
            self.db.delete(parent)
            self.db.flush()
        for neighbor_id, attribute, target in (
            (previous_id, "next_chunk_id", next_id),
            (next_id, "prev_chunk_id", previous_id),
        ):
            neighbor = self.db.get(KnowledgeChunk, neighbor_id) if neighbor_id else None
            if (
                neighbor is not None
                and neighbor.document_id == document_id
                and neighbor.tenant_id == row.tenant_id
            ):
                setattr(neighbor, attribute, target)
        self.db.delete(row)
        self.db.flush()
        if document_id:
            document = require_scope(self.db.get(KnowledgeDocument, document_id), user)
            remaining = self.db.query(KnowledgeChunk).filter_by(document_id=document_id).count()
            if remaining:
                document.status = "partial_index"
                document.meta = {
                    **(document.meta or {}),
                    "index_modified_at": datetime.now(timezone.utc).isoformat(),
                    "deleted_chunk_ids": [
                        *(document.meta or {}).get("deleted_chunk_ids", []),
                        chunk_id,
                    ],
                }
                if owner is not None:
                    owner.index_status = "stale"
                    owner.index_error = "部分叶子索引已删除；相关父块已失效，可审核后重建"
            else:
                if owner is not None:
                    owner.indexed_document_id, owner.index_status, owner.index_error = (
                        None,
                        "removed",
                        None,
                    )
                self.db.delete(document)
        self.db.commit()
        return {"deleted": chunk_id}

    def structure_preview(self, document_id: str, user: User) -> dict[str, Any]:
        document = require_scope(
            self.db.query(KnowledgeDocument)
            .filter_by(id=document_id)
            .with_for_update(read=True)
            .first(),
            user,
        )
        try:
            return derive(document, active_rows(self.db, document))
        except ValueError as exc:
            raise InvalidKnowledgeError(str(exc)) from exc

    def review_structure(
        self, document_id: str, body: StructureReview, user: User
    ) -> dict[str, Any]:
        self._lock_index_owner(document_id, user)
        document = require_scope(
            self.db.query(KnowledgeDocument).filter_by(id=document_id).with_for_update().first(),
            user,
        )
        if not body.source_reviewed or document.status != "indexed":
            raise InvalidKnowledgeError("需核对来源且索引完整；部分删除资料必须先重建")
        try:
            rows = active_rows(self.db, document)
            current = derive(document, rows)
        except ValueError as exc:
            raise InvalidKnowledgeError(str(exc)) from exc
        if (
            body.expected_index_revision != current["index_revision"]
            or body.review_signature != current["review_signature"]
        ):
            raise InvalidKnowledgeError("结构/活动索引版本变化，请刷新后审核")
        try:
            requested = derive(document, rows, body.accepted_edge_ids, body.rejected_edge_ids)
        except ValueError as exc:
            raise InvalidKnowledgeError(str(exc)) from exc
        document.meta = {
            **(document.meta or {}),
            "structure_review": {
                "accepted_edge_ids": sorted(set(body.accepted_edge_ids)),
                "rejected_edge_ids": sorted(set(body.rejected_edge_ids)),
                "review_signature": requested["review_signature"],
                "source_scope_signature": requested["source_scope_signature"],
                "version": requested["version"],
                "policy_hash": requested["policy_hash"],
                "reviewer_id": user.id,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "revision": current["structure_revision"] + 1,
            },
        }
        self.db.commit()
        result = derive(document, rows)
        result.update(embedding_calls=0, index_rebuilt=False)
        return result

    def retrieve_knowledge(
        self,
        query: str,
        knowledge_point: str | None,
        top_k: int | None,
        user: User,
        scope_mode: ScopeMode | None = None,
        document_ids: list[str] | None = None,
        source_name: str | None = None,
        section_path: list[str] | None = None,
        context_mode: str | None = None,
    ) -> RetrievalResult:
        citations: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {}
        snippets = retrieve(
            self.db,
            query=query,
            knowledge_point=knowledge_point,
            top_k=top_k,
            tenant_id=user.tenant_id,
            allow_all_tenants=user.role == "admin",
            details=citations,
            diagnostics=diagnostics,
            scope_mode=scope_mode,
            document_ids=document_ids,
            source_name=source_name,
            section_path=section_path,
            context_mode=context_mode,
        )
        return {
            "query": query,
            "snippets": snippets,
            "citations": citations,
            "diagnostics": diagnostics,
            "protocol_version": str(diagnostics.get("protocol_version", "rag-context-v1")),
            "bundles": diagnostics.get("context_bundles", []),
        }
