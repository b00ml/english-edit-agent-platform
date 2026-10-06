"""Parent/adjacent context retrieval with scope checks, de-duplication and hard budgets."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk
from app.rag.knowledge_points import KnowledgeScope
from app.rag.retrieval.models import Candidate, chunk_metadata, metadata, predicates
from app.versioning import hash_value


def citation(row: KnowledgeChunk, candidate: Candidate) -> dict[str, Any]:
    data = chunk_metadata(row)
    return {
        "chunk_id": row.id,
        "source_name": row.source_name,
        "source_type": row.source_type,
        "content": row.content,
        "content_hash": hash_value(row.content),
        "similarity": candidate.similarity,
        "keyword_score": candidate.keyword_score,
        "rrf_score": candidate.rrf_score,
        "rerank_score": candidate.rerank_score,
        "retrieval_ranks": candidate.ranks,
        "eligible": True,
        "verification": "unverified",  # factual correctness is not established by source QA
        "source_reviewed": metadata(row).get("source_verified") is True,
        "source_review_job_id": metadata(row).get("ocr_job_id"),
        "document_id": row.document_id,
        "parent_chunk_id": row.parent_chunk_id,
        "chunk_type": row.chunk_type or "legacy",
        "context_header": row.context_header or "",
        "page_no": row.page_no,
        "content_start": row.content_start,
        "content_end": row.content_end,
        "section_path": row.section_path or [],
        "source_locator": data.get("source_locator", []),
        "table_id": data.get("table_id"),
        "row_range": data.get("row_range"),
        "source_hash": metadata(row).get("source_hash"),
        "parser_version": row.parser_version,
        "chunker_version": row.chunker_version,
        "embedding_content_hash": row.embedding_content_hash,
        "legacy": row.document_id is None,
        "indexed_source_segments": data.get("source_segments", []),
        "pages": data.get("pages", []),
        "matched_child_ids": [candidate.row.id],
        "context_role": (
            "matched"
            if row.id == candidate.row.id
            else "parent" if row.chunk_type == "parent" else "neighbor"
        ),
        "similarity_kind": "matched_child",
        "match_chunk_id": candidate.row.id,
        "truncated": False,
    }


def format_source(text: str, info: dict[str, Any]) -> str:
    return f"[来源 {info['chunk_id']}: {info['source_name']}]\n{text}"


def _row(
    session: Session,
    row_id: str | None,
    document_id: str | None,
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
    diagnostics: dict[str, Any] | None = None,
) -> KnowledgeChunk | None:
    if not row_id or not document_id:
        return None
    stmt = select(KnowledgeChunk).where(
        KnowledgeChunk.id == row_id, *predicates(scope, tenant, allow_all, session, [document_id])
    )
    try:
        transaction = session.begin_nested() if hasattr(session, "begin_nested") else nullcontext()
        with transaction:
            return session.execute(stmt).scalars().first()
    except SQLAlchemyError as exc:
        if diagnostics is not None:
            diagnostics.setdefault("fallbacks", []).append(
                {"stage": "context_expansion", "error_type": type(exc).__name__}
            )
        return None


def _compatible(left: KnowledgeChunk, right: KnowledgeChunk) -> bool:
    return (
        left.document_id == right.document_id
        and left.tenant_id == right.tenant_id
        and left.page_no == right.page_no
        and left.section_path == right.section_path
        and left.knowledge_point_ids == right.knowledge_point_ids
        and left.knowledge_point == right.knowledge_point
        and left.knowledge_point_labels == right.knowledge_point_labels
    )


def expand(
    session: Session,
    candidates: list[Candidate],
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
    top_k: int,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    selected: dict[str, tuple[KnowledgeChunk, Candidate]] = {}
    neighbors: dict[str, tuple[KnowledgeChunk, Candidate]] = {}
    cache: dict[tuple[str | None, str | None], KnowledgeChunk | None] = {}

    def lookup(row_id: str | None, document_id: str | None) -> KnowledgeChunk | None:
        key = (row_id, document_id)
        if key not in cache:
            cache[key] = _row(session, row_id, document_id, scope, tenant, allow_all, diagnostics)
        return cache[key]

    for candidate in candidates:
        leaf = candidate.row
        parent = lookup(leaf.parent_chunk_id, leaf.document_id)
        if parent is not None and parent.chunk_type == "parent" and _compatible(leaf, parent):
            row = parent
        else:
            row = leaf
        if row.id not in selected:
            selected[row.id] = (row, candidate)
        # A full parent already covers its children; do not inflate the context.
        if row is leaf and leaf.document_id:
            for direction in ["prev_chunk_id", "next_chunk_id"]:
                current = leaf
                for _ in range(settings.RAG_NEIGHBOR_WINDOW):
                    neighbor = lookup(getattr(current, direction), leaf.document_id)
                    if (
                        neighbor is None
                        or neighbor.chunk_type == "parent"
                        or not _compatible(leaf, neighbor)
                    ):
                        break
                    if len(selected) + len(neighbors) >= settings.RAG_CONTEXT_EXPANSION_LIMIT:
                        break
                    neighbors.setdefault(neighbor.id, (neighbor, candidate))
                    current = neighbor
        if len(selected) >= settings.RAG_CONTEXT_EXPANSION_LIMIT:
            break
    selected.update({key: value for key, value in neighbors.items() if key not in selected})
    selected = dict(list(selected.items())[: settings.RAG_CONTEXT_EXPANSION_LIMIT])
    # Remove children already covered by an expanded parent.
    parents = {key for key, (row, _) in selected.items() if row.chunk_type == "parent"}
    selected = {
        key: value for key, value in selected.items() if value[0].parent_chunk_id not in parents
    }
    texts: list[str] = []
    citations: list[dict[str, Any]] = []
    used_chars = used_bytes = 0
    seen_spans: set[tuple[str | None, int | None, int | None]] = set()
    occupied: dict[str, list[tuple[int, int]]] = {}
    for row, candidate in selected.values():
        if len(texts) >= top_k:
            break
        span = (row.document_id, row.content_start, row.content_end)
        if row.document_id and span in seen_spans:
            continue
        info = citation(row, candidate)
        matches = [
            c.row.id for c in candidates if c.row.id == row.id or c.row.parent_chunk_id == row.id
        ]
        info["matched_child_ids"] = list(dict.fromkeys(matches or [candidate.row.id]))
        header = row.context_header or ""
        body = row.content
        if row.document_id and row.content_start is not None and row.content_end is not None:
            remaining = [(row.content_start, row.content_end)]
            for lower, upper in occupied.get(row.document_id, []):
                remaining = [
                    (a, b)
                    for start, end in remaining
                    for a, b in [(start, min(end, lower)), (max(start, upper), end)]
                    if a < b
                ]
            if not remaining:
                continue
            start, end = max(remaining, key=lambda bounds: bounds[1] - bounds[0])
            body = row.content[start - row.content_start : end - row.content_start]
            info.update(
                content=body,
                content_start=start,
                content_end=end,
                content_hash=hash_value(body),
                overlap_trimmed=start != row.content_start or end != row.content_end,
            )
        separator_size = 2 if texts else 0

        def fits(value: str) -> bool:
            rendered = f"{header}\n\n{value}" if header else value
            wrapped = format_source(rendered, info)
            return used_chars + separator_size + len(
                wrapped
            ) <= settings.RAG_CONTEXT_MAX_CHARS and (
                settings.RAG_CONTEXT_TOKEN_LIMIT is None
                or used_bytes + separator_size + len(wrapped.encode("utf-8"))
                <= settings.RAG_CONTEXT_TOKEN_LIMIT
            )

        if not fits(body):
            low, high = 0, len(body)
            while low < high:
                middle = (low + high + 1) // 2
                if fits(body[:middle]):
                    low = middle
                else:
                    high = middle - 1
            if low < 16:
                continue
            info["original_content_hash"] = info["content_hash"]
            body = body[:low]
            info.update(content=body, content_hash=hash_value(body), truncated=True)
            if info["content_start"] is not None:
                info["content_end"] = info["content_start"] + len(body)
        rendered = f"{header}\n\n{body}" if header else body
        wrapped = format_source(rendered, info)
        used_chars += separator_size + len(wrapped)
        used_bytes += separator_size + len(wrapped.encode("utf-8"))
        texts.append(rendered)
        citations.append(info)
        seen_spans.add(span)
        if (
            row.document_id
            and info["content_start"] is not None
            and info["content_end"] is not None
        ):
            occupied.setdefault(row.document_id, []).append(
                (info["content_start"], info["content_end"])
            )
    return texts, citations
