"""Read-only derivation of an active index's structure; never fetch raw file/OCR/models."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag.document import DocumentBlock, ParsedDocument
from app.rag.retrieval.models import leaf_predicate
from app.rag.structure.builder import STRUCTURE_VERSION, build_structure
from app.versioning import hash_value


def document_snapshot(document: KnowledgeDocument) -> ParsedDocument:
    return {
        "source_name": document.source_name,
        "source_type": document.source_type,
        "source_hash": document.source_hash,
        "parser_version": document.parser_version,
        "blocks": cast(list[DocumentBlock], document.blocks),
        "warnings": document.warnings,
        "stats": document.stats,
        "text": document.normalized_text,
        "original_text": document.original_text,
    }


def source_scope_signature(document: KnowledgeDocument, rows: list[KnowledgeChunk]) -> str:
    return hash_value(
        {
            "document_id": document.id,
            "tenant_id": document.tenant_id,
            "status": document.status,
            "index_revision": (document.meta or {}).get("index_revision", 1),
            "source_hash": document.source_hash,
            "text_hash": hash_value(document.normalized_text),
            "rows": sorted(
                [
                    [
                        row.id,
                        hash_value(row.content),
                        row.content_start,
                        row.content_end,
                        row.chunk_type,
                        row.tenant_id,
                        row.knowledge_point,
                        row.knowledge_point_ids,
                        row.knowledge_point_labels,
                        hash_value((row.meta or {}).get("chunk", {}).get("source_segments", [])),
                    ]
                    for row in rows
                ],
                key=lambda value: str(value[0]),
            ),
        }
    )


def active_rows(session: Session, document: KnowledgeDocument) -> list[KnowledgeChunk]:
    rows = session.scalars(
        select(KnowledgeChunk)
        .where(
            KnowledgeChunk.document_id == document.id,
            KnowledgeChunk.tenant_id == document.tenant_id,
            leaf_predicate(),
        )
        .limit(settings.RAG_STRUCTURE_MAX_LEAFS + 1)
    ).all()
    if len(rows) > settings.RAG_STRUCTURE_MAX_LEAFS:
        raise ValueError("Active leaf limit exceeded")
    return list(rows)


def derive(
    document: KnowledgeDocument,
    rows: list[KnowledgeChunk],
    accepted_edge_ids: list[str] | None = None,
    rejected_edge_ids: list[str] | None = None,
) -> dict[str, Any]:
    decisions = (document.meta or {}).get("structure_review") or (document.meta or {}).get(
        "structure_decisions", {}
    )
    stale_decisions = bool(decisions.get("version") and decisions["version"] != STRUCTURE_VERSION)
    previous_decisions = dict(decisions) if stale_decisions else None
    if stale_decisions:
        decisions = {"revision": decisions.get("revision", 0)}
    accepted = (
        accepted_edge_ids
        if accepted_edge_ids is not None
        else decisions.get("accepted_edge_ids", [])
    )
    rejected = (
        rejected_edge_ids
        if rejected_edge_ids is not None
        else decisions.get("rejected_edge_ids", [])
    )
    plan = build_structure(
        document_snapshot(document),
        accepted_edge_ids=accepted,
        rejected_edge_ids=rejected,
        max_blocks=settings.RAG_STRUCTURE_MAX_BLOCKS,
    )
    plan["previous_version_decisions"] = previous_decisions
    plan["source_scope_signature"] = source_scope_signature(document, rows)
    plan["review_signature"] = hash_value([plan["signature"], plan["source_scope_signature"]])
    plan["index_revision"] = int((document.meta or {}).get("index_revision", 1))
    plan["structure_revision"] = int(decisions.get("revision", 0))
    plan["expansion_allowed"] = document.status == "indexed"
    return plan
