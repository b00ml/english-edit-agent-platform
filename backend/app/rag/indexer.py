"""RAG chunk/embed persistence; one atomic write after complete vector validation."""

from __future__ import annotations

import math
from typing import Any, List, Literal, cast

from pgvector.sqlalchemy import Vector
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import InvalidKnowledgeError
from app.models import KnowledgeChunk, KnowledgeDocument, new_uuid
from app.rag.chunking import Chunk, ChunkingConfig, split_document, split_text
from app.rag.chunking.parents import parent_plans
from app.rag.document import ParsedDocument, text_hash
from app.rag.embedding import embed_texts
from app.rag.knowledge_points import load_catalog, normalized
from app.rag.parser import parse_text
from app.rag.structure import build_structure


def configured_chunking(layout: str | None = None) -> ChunkingConfig:
    return ChunkingConfig(
        strategy=settings.RAG_CHUNK_STRATEGY,
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
        min_chunk_chars=settings.RAG_CHUNK_MIN_CHARS,
        token_limit=settings.RAG_CHUNK_TOKEN_LIMIT,
        layout=cast(Literal["legacy", "structure"], layout or settings.RAG_CHUNK_LAYOUT),
    )


def chunk_text(text: str, max_chars: int = 500, overlap: int = 50) -> List[str]:
    chunks, _ = split_text(
        text,
        ChunkingConfig(
            strategy="legacy",
            chunk_size=max_chars,
            chunk_overlap=overlap,
            min_chunk_chars=min(80, max_chars),
        ),
    )
    return [chunk.embedding_content.strip() for chunk in chunks]


def index_document(
    session: Session,
    source_type: str,
    source_name: str,
    text: str | ParsedDocument,
    knowledge_point: str | None = None,
    meta: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    details: dict[str, Any] | None = None,
    knowledge_points: list[str] | None = None,
    document_id: str | None = None,
    commit: bool = True,
    replace_existing: bool = False,
    structure_decisions: dict[str, list[str]] | None = None,
    chunk_layout: str | None = None,
) -> int:
    if settings.EMBEDDING_DIM != cast(Vector, KnowledgeChunk.__table__.c.embedding.type).dim:
        raise InvalidKnowledgeError("Embedding 维度与数据库 Schema 不一致，必须先迁移/调整配置")
    document = text if isinstance(text, dict) else parse_text(f"{source_name}.md", text)
    cfg = configured_chunking(chunk_layout)
    decisions = structure_decisions or {}
    structure = (
        build_structure(
            document,
            accepted_edge_ids=decisions.get("accepted_edge_ids"),
            rejected_edge_ids=decisions.get("rejected_edge_ids"),
            max_blocks=settings.RAG_STRUCTURE_MAX_BLOCKS,
        )
        if settings.RAG_STRUCTURE_ENABLED or cfg.layout == "structure"
        else None
    )
    if cfg.layout == "structure":
        from app.rag.chunking.structural import split_structural

        chunks, diagnostics = split_structural(document, cfg, structure)
    else:
        chunks, diagnostics = split_document(document, cfg)
    if not chunks:
        return 0
    if settings.RAG_EMBEDDING_BATCH_SIZE <= 0:
        raise InvalidKnowledgeError("RAG_EMBEDDING_BATCH_SIZE 必须大于0")
    catalog = load_catalog()
    primary = catalog.resolve(knowledge_point) if knowledge_point else None
    knowledge_point = primary.canonical_name if primary else knowledge_point
    point_ids, point_labels = catalog.tags(
        ([knowledge_point] if knowledge_point else []) + (knowledge_points or [])
    )
    point_labels = [normalized(label) for label in point_labels]
    parents = parent_plans(document, chunks)
    parent_ids = [new_uuid() for _ in parents]
    child_ids = [new_uuid() for _ in chunks]
    parent_by_child = {
        index: parent_ids[p] for p, plan in enumerate(parents) for index in plan.child_indexes
    }
    embedding_inputs = [chunk.embedding_content for chunk in chunks]
    document_id = document_id or new_uuid()
    existing = session.get(KnowledgeDocument, document_id)
    if existing is not None and (not replace_existing or existing.tenant_id != tenant_id):
        raise InvalidKnowledgeError("文档已存在或归属不一致，未调用embedding")
    vectors: list[list[float]] = []
    batch_size = min(settings.RAG_EMBEDDING_BATCH_SIZE, settings.EMBEDDING_PROVIDER_BATCH_LIMIT)
    for start in range(0, len(chunks), batch_size):
        inputs = embedding_inputs[start : start + batch_size]
        batch = embed_texts(inputs, tenant_id=tenant_id, trace_id=f"knowledge:{document_id}")
        if len(batch) != len(inputs) or any(
            len(vec) != settings.EMBEDDING_DIM
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vec)
            for vec in batch
        ):
            raise InvalidKnowledgeError("向量返回数量或维度不匹配，未入库")
        vectors.extend(batch)
    document_meta = {
        **dict(meta or {}),
        "chunk_diagnostics": diagnostics.as_dict(),
        "parent_count": len(parents),
        "structure": (
            {key: structure[key] for key in ("version", "signature", "policy_hash", "decisions")}
            if structure
            else None
        ),
        "knowledge_catalog_hash": catalog.hash,
        "indexer_version": "rag-str5-v1",
        "chunk_layout": cfg.layout,
        "index_input_signature": diagnostics.plan_signature
        or text_hash("\n".join(text_hash(chunk.embedding_content) for chunk in chunks)),
    }
    if structure and any(decisions.values()):
        document_meta["structure_decisions"] = decisions
    row = KnowledgeDocument(
        id=document_id,
        tenant_id=tenant_id,
        source_name=source_name,
        source_type=source_type,
        content_hash=text_hash(document["text"]),
        source_hash=document["source_hash"]
        or text_hash(document["original_text"] or document["text"]),
        parser_version=document["parser_version"],
        status="indexed",
        normalized_text=document["text"],
        original_text=document["original_text"],
        blocks=document["blocks"],
        warnings=document["warnings"],
        stats={**document["stats"], "chunk_count": len(chunks)},
        meta=document_meta,
    )

    def make_row(
        chunk: Chunk,
        row_id: str,
        vector: list[float] | None,
        index: int | None,
        kind: str,
        parent_id: str | None,
    ) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=row_id,
            tenant_id=tenant_id,
            source_type=source_type,
            source_name=source_name,
            knowledge_point=knowledge_point,
            knowledge_point_ids=point_ids,
            knowledge_point_labels=point_labels,
            content=chunk.content,
            embedding=vector,
            document_id=document_id,
            parent_chunk_id=parent_id,
            chunk_type=kind,
            chunk_index=index,
            content_hash=text_hash(chunk.content),
            search_text=normalized(
                " ".join(
                    [
                        source_name,
                        *(point_labels or []),
                        *chunk.section_path,
                        chunk.embedding_content,
                    ]
                )
            ),
            content_start=None if chunk.source_segments else chunk.start,
            content_end=None if chunk.source_segments else chunk.end,
            page_no=chunk.page_no,
            context_header=chunk.context_header,
            section_path=chunk.section_path,
            parser_version=document["parser_version"],
            chunker_version=chunk.chunker_version,
            embedding_model=settings.EMBEDDING_MODEL_NAME if vector is not None else None,
            embedding_dimension=settings.EMBEDDING_DIM if vector is not None else None,
            embedding_content_hash=(
                text_hash(chunk.embedding_content) if vector is not None else None
            ),
            meta={
                **dict(meta or {}),
                "chunk": chunk.as_meta(),
                "source_hash": row.source_hash,
                "parser_warnings": document["warnings"],
                "chunk_diagnostics": diagnostics.as_dict(),
                "knowledge_catalog_hash": catalog.hash,
                "indexer_version": "rag-p1-def-v1",
            },
        )

    try:
        if existing is None:
            session.add(row)
        else:
            # Short commit-phase lock only: queries may keep serving the old index during embedding.
            session.refresh(existing, with_for_update=True)
            # Compute/validate all vectors before removing any live rows. Atomic swap on commit.
            session.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).delete(
                synchronize_session=False
            )
            for column in KnowledgeDocument.__table__.columns:
                if column.name not in {"id", "created_at"}:
                    setattr(existing, column.name, getattr(row, column.name))
        session.flush()  # Enforce document FK ordering without ORM relationships.
        for plan, parent_id in zip(parents, parent_ids):
            session.add(make_row(plan.chunk, parent_id, None, None, "parent", None))
        session.flush()
        child_rows = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            assigned_parent = parent_by_child.get(index)
            child_row = make_row(
                chunk,
                child_ids[index],
                vector,
                index,
                "child" if assigned_parent else "single",
                assigned_parent,
            )
            session.add(child_row)
            child_rows.append(child_row)
        session.flush()
        for index, child_row in enumerate(child_rows):
            child_row.prev_chunk_id = child_ids[index - 1] if index else None
            child_row.next_chunk_id = child_ids[index + 1] if index + 1 < len(child_ids) else None
        if commit:
            session.commit()
        else:
            session.flush()
    except SQLAlchemyError:  # Roll back the entire ingestion, preserving the original failure
        session.rollback()
        raise
    if details is not None:
        details.update(
            document_id=document_id,
            warnings=document["warnings"],
            diagnostics={**diagnostics.as_dict(), "parent_count": len(parents)},
            canonical_knowledge_point=knowledge_point,
        )
    return len(chunks)
