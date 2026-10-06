"""Audit actual returned source segments; bundle membership is not delivered evidence."""

from __future__ import annotations

from typing import Any


def delivered_citations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        segment
        for entry in entries
        for segment in (
            entry.get("source_segments", [])
            if entry.get("protocol_version") == "rag-context-v2"
            else [entry]
        )
    ]


def reference_snapshot(citations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "chunk_ids": list(dict.fromkeys(citation["chunk_id"] for citation in citations)),
        "segment_ids": [
            citation["segment_id"] for citation in citations if citation.get("segment_id")
        ],
        "source_snapshots": [
            {
                key: citation.get(key)
                for key in (
                    "segment_id",
                    "chunk_id",
                    "document_id",
                    "page_no",
                    "block_id",
                    "content_hash",
                    "source_content_hash",
                    "content_start",
                    "content_end",
                    "structure_signature",
                    "source_scope_signature",
                    "index_revision",
                )
            }
            for citation in citations
        ],
    }
