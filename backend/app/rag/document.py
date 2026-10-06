"""JSON-serialisable parser output; offsets refer to normalized document text."""

from __future__ import annotations

import hashlib
from typing import Any, Literal, TypedDict

BlockType = Literal[
    "paragraph", "heading", "table", "table_row", "list", "code", "image", "page_break"
]


class DocumentBlock(TypedDict):
    block_id: str
    block_type: BlockType
    text: str
    level: int | None
    page_no: int | None
    order: int
    source_locator: dict[str, Any]  # Format-specific, JSON-serialisable location.
    meta: dict[str, Any]  # Table cells, spans, section hints, and original snapshots.


class ParsedDocument(TypedDict):
    source_name: str
    source_type: str
    parser_version: str
    blocks: list[DocumentBlock]
    warnings: list[str]
    stats: dict[str, Any]
    text: str
    source_hash: str
    original_text: str | None


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finalize_document(document: ParsedDocument) -> ParsedDocument:
    """Assign actual normalized offsets, not offsets in concatenated chunks."""
    cursor = 0
    parts = []
    for block in document["blocks"]:
        block["meta"]["content_start"] = cursor
        parts.append(block["text"])
        cursor += len(block["text"])
        block["meta"]["content_end"] = cursor
        cursor += 2
    document["text"] = "\n\n".join(parts)
    document["stats"].update(
        block_count=len(document["blocks"]),
        non_empty_chars=sum(not char.isspace() for char in document["text"]),
        normalized_content_hash=text_hash(document["text"]),
    )
    return document


def block_text(document: ParsedDocument) -> str:
    return document["text"]
