"""Common candidate contract and explicit tenant/knowledge/document predicates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Text, bindparam, exists, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import KnowledgeChunk
from app.rag.knowledge_points import KnowledgeScope, normalized


@dataclass
class Candidate:
    row: KnowledgeChunk
    similarity: float | None = None
    keyword_score: float | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None
    ranks: dict[str, int] = field(default_factory=dict)


def dialect_name(session: Session) -> str:
    bind = getattr(session, "bind", None)
    return bind.dialect.name if bind is not None else "postgresql"


def predicates(
    scope: KnowledgeScope,
    tenant_id: str | None,
    allow_all: bool,
    session: Session,
    document_ids: list[str] | None = None,
) -> list[Any]:
    filters = []
    if not allow_all:
        filters.append(KnowledgeChunk.tenant_id == tenant_id)
    if document_ids is not None:
        filters.append(KnowledgeChunk.document_id.in_(document_ids))
    if scope.source_name:
        filters.append(KnowledgeChunk.source_name == scope.source_name)
    for index, label in enumerate(scope.section_path):
        if dialect_name(session) == "sqlite":
            filters.append(func.json_extract(KnowledgeChunk.section_path, f"$[{index}]") == label)
        else:
            filters.append(KnowledgeChunk.section_path[index].astext == label)
    if scope.ids or scope.labels:
        alternatives: list[ColumnElement[bool]] = [
            func.lower(func.trim(KnowledgeChunk.knowledge_point)).in_(scope.labels)
        ]
        if dialect_name(session) == "sqlite":
            ids = func.json_each(KnowledgeChunk.knowledge_point_ids).table_valued("value")
            labels = func.json_each(KnowledgeChunk.knowledge_point_labels).table_valued("value")
            alternatives.extend(
                [
                    exists(select(1).select_from(ids).where(ids.c.value.in_(scope.ids))),
                    exists(
                        select(1)
                        .select_from(labels)
                        .where(func.lower(labels.c.value).in_(scope.labels))
                    ),
                ]
            )
        else:
            alternatives.extend(
                KnowledgeChunk.knowledge_point_ids.contains([key]) for key in scope.ids
            )
            alternatives.append(
                KnowledgeChunk.knowledge_point_labels.has_any(
                    bindparam(None, scope.labels, type_=ARRAY(Text()))
                )
            )
        filters.append(or_(*alternatives))
    return filters


def leaf_predicate() -> Any:
    return or_(
        KnowledgeChunk.chunk_type.is_(None), KnowledgeChunk.chunk_type != literal_column("'parent'")
    )


def metadata(row: KnowledgeChunk) -> dict[str, Any]:
    return row.meta if isinstance(row.meta, dict) else {}


def chunk_metadata(row: KnowledgeChunk) -> dict[str, Any]:
    value = metadata(row).get("chunk", {})
    return value if isinstance(value, dict) else {}


def search_text(row: KnowledgeChunk) -> str:
    return row.search_text or normalized(
        " ".join(
            value
            for value in [
                row.source_name,
                row.knowledge_point,
                row.context_header,
                row.content,
            ]
            if value
        )
    )


def candidate_summary(candidate: Candidate) -> dict[str, Any]:
    """Bounded identity/rank evidence, never candidate bodies or provider credentials."""
    row = candidate.row
    data = chunk_metadata(row)
    return {
        "chunk_id": row.id,
        "source_block_ids": data.get("block_ids", []),
        "diagnostic_candidate": True,
        "pages": data.get("pages", []) or ([row.page_no] if row.page_no is not None else []),
        "document_id": row.document_id,
        "page_no": row.page_no,
        "table_id": data.get("table_id"),
        "row_range": data.get("row_range"),
        "similarity": candidate.similarity,
        "keyword_score": candidate.keyword_score,
    }
