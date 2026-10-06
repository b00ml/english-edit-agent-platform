"""Validate source coverage and budgets rather than counting duplicated characters."""

from __future__ import annotations

from app.rag.chunking.models import Chunk, ChunkingConfig


def validate_chunks(chunks: list[Chunk], text: str, cfg: ChunkingConfig) -> list[str]:
    errors = []
    last_start = -1
    for chunk in chunks:
        if not chunk.content.strip():
            errors.append("empty_chunk")
        if not 0 <= chunk.start < chunk.end <= len(text):
            errors.append("invalid_source_range")
        elif chunk.content != text[chunk.start : chunk.end]:
            errors.append("content_does_not_match_source")
        if chunk.start < last_start:
            errors.append("non_monotonic_range")
        last_start = chunk.start
        if len(chunk.embedding_content) > cfg.chunk_size:
            errors.append("character_budget_exceeded")
        if cfg.token_limit and len(chunk.embedding_content.encode("utf-8")) > cfg.token_limit:
            errors.append("conservative_token_budget_exceeded")
    if coverage_ratio(chunks, text) < 1.0:
        errors.append("incomplete_non_whitespace_coverage")
    return list(dict.fromkeys(errors))


def coverage_ratio(chunks: list[Chunk], text: str) -> float:
    total = sum(not char.isspace() for char in text)
    if not total:
        return 1.0
    covered = bytearray(len(text))
    for chunk in chunks:
        start, end = max(0, chunk.start), min(len(text), chunk.end)
        covered[start:end] = b"\x01" * max(0, end - start)
    return (
        sum(bool(covered[index]) and not char.isspace() for index, char in enumerate(text)) / total
    )
