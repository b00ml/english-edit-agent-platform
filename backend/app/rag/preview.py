"""Pure structured document preview shared by native HTTP preview and offline OCR."""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.errors import ChunkingError, InvalidKnowledgeError
from app.rag.chunking import split_document
from app.rag.chunking.parents import parent_plans
from app.rag.document import ParsedDocument
from app.rag.indexer import configured_chunking
from app.rag.structure import build_structure


def preview_document(
    document: ParsedDocument,
    accepted_edge_ids: list[str] | None = None,
    rejected_edge_ids: list[str] | None = None,
    chunk_layout: str | None = None,
) -> dict[str, Any]:
    cfg = configured_chunking(chunk_layout)
    try:
        structure = (
            build_structure(
                document,
                accepted_edge_ids=accepted_edge_ids,
                rejected_edge_ids=rejected_edge_ids,
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
    except (ChunkingError, ValueError) as exc:
        raise InvalidKnowledgeError(str(exc)) from exc
    partial = bool(document["stats"].get("partial_document"))
    return {
        "indexable": bool(chunks) and not partial,
        "reason_code": (
            "KNOWLEDGE_PARTIAL_DOCUMENT"
            if partial
            else None if chunks else "KNOWLEDGE_NO_INDEXABLE_TEXT"
        ),
        "message": (
            "仅为所选页预览，尚未完成全文件解析，当前不可正式入库"
            if partial
            else (
                None
                if chunks
                else "未提取到可索引文字。扫描 PDF 需先进行 OCR；当前仅保留页码和解析提示。"
            )
        ),
        "document": document,
        "structure": structure,
        "chunk_layout": cfg.layout,
        "chunks": [
            {
                "content": chunk.content,
                "embedding_content": chunk.embedding_content,
                **chunk.as_meta(),
            }
            for chunk in chunks
        ],
        "parents": [
            {
                "content": plan.chunk.content,
                "child_indexes": plan.child_indexes,
                **plan.chunk.as_meta(),
            }
            for plan in parent_plans(document, chunks)
        ],
        "diagnostics": diagnostics.as_dict(),
    }
