"""Adaptive strategy/fallback pipeline ported to the project's Python data contracts.

Design reference: WeKnora strategy.go, splitter.go, header_tracker.go.
No upstream Go runtime or third-party parser code is imported.
"""

from __future__ import annotations

import re
from typing import Any

from app.errors import ChunkingError
from app.rag import tables
from app.rag.chunking.models import Chunk, ChunkDiagnostics, ChunkingConfig
from app.rag.chunking.validator import coverage_ratio, validate_chunks
from app.rag.document import DocumentBlock, ParsedDocument
from app.rag.parser import parse_text

_TIERS = ("heading", "heuristic", "recursive", "legacy")


def split_text(
    text: str, config: ChunkingConfig | None = None
) -> tuple[list[Chunk], ChunkDiagnostics]:
    return split_document(parse_text("inline.md", text), config)


def split_document(
    document: ParsedDocument,
    config: ChunkingConfig | None = None,
) -> tuple[list[Chunk], ChunkDiagnostics]:
    cfg = (config or ChunkingConfig()).normalized()
    if cfg.layout == "structure":
        from app.rag.chunking.structural import split_structural

        return split_structural(document, cfg)
    tiers = _TIERS if cfg.strategy == "auto" else _TIERS[_TIERS.index(cfg.strategy) :]
    attempts: list[dict[str, str]] = []
    for tier in tiers:
        try:
            if tier == "heading" and not any(
                b["block_type"] == "heading" for b in document["blocks"]
            ):
                raise ChunkingError("document_has_no_headings")
            chunks = _run_tier(document, cfg, tier)
            reasons = validate_chunks(chunks, document["text"], cfg)
            if reasons:
                raise ChunkingError(",".join(reasons))
        except (ChunkingError, ValueError) as exc:
            attempts.append({"strategy": tier, "reason": str(exc)})
            continue
        lengths = [len(chunk.content) for chunk in chunks]
        warnings = [message for chunk in chunks for message in chunk.warning]
        if cfg.chunk_overlap != (config or ChunkingConfig()).chunk_overlap:
            warnings.append("overlap 已限制为 chunk_size 的一半")
        if any(len(chunk.content) < cfg.min_chunk_chars for chunk in chunks[:-1]):
            warnings.append("部分结构边界小块低于 min_chunk_chars；为保留页/表/章节边界未强制拼接")
        diagnostics = ChunkDiagnostics(
            strategy_requested=cfg.strategy,
            strategy_used=tier,
            chunk_count=len(chunks),
            min_length=min(lengths, default=0),
            max_length=max(lengths, default=0),
            overlap_actual=min(
                (max(0, a.end - b.start) for a, b in zip(chunks, chunks[1:])), default=0
            ),
            coverage_ratio=coverage_ratio(chunks, document["text"]),
            table_chunks=sum(bool(chunk.table_id) for chunk in chunks),
            hard_splits=sum(chunk.hard_split for chunk in chunks),
            max_embedding_bytes=max(
                (len(c.embedding_content.encode("utf-8")) for c in chunks), default=0
            ),
            warnings=list(dict.fromkeys(warnings)),
            fallback_attempts=attempts,
        )
        return chunks, diagnostics
    raise ChunkingError(f"所有切块策略失败: {attempts}")


def _run_tier(document: ParsedDocument, cfg: ChunkingConfig, tier: str) -> list[Chunk]:
    text = document["text"]
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []
    pending: list[DocumentBlock] = []
    pending_context = ""
    path: list[str] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            chunks.extend(
                _range_chunks(
                    text,
                    int(pending[0]["meta"]["content_start"]),
                    int(pending[-1]["meta"]["content_end"]),
                    pending,
                    cfg,
                    tier,
                    pending_context,
                    list(path),
                )
            )
            pending = []

    for block in document["blocks"]:
        if block["meta"].get("reset_section_context"):
            flush()
            headings = []
            path = []
            pending_context = ""
        if not block["text"].strip():
            flush()
            continue
        if block["block_type"] == "heading":
            level = block["level"] or 1
            # Keep a parent -> child heading chain with its following body,
            # rather than embedding an isolated, high-similarity title chunk.
            heading_chain = pending and all(b["block_type"] == "heading" for b in pending)
            if not (heading_chain and headings and level > headings[-1][0]):
                flush()
            while headings and headings[-1][0] >= level:
                headings.pop()
            title = str(block["meta"].get("title") or re.sub(r"^#+\s*", "", block["text"]))
            headings.append((level, title))
            path = [title for _, title in headings]
        context = " > ".join(path)
        block["meta"]["heading_level"] = headings[-1][0] if headings else None
        if block["block_type"] == "heading":
            pending_context = context
        if block["block_type"] == "table" and cfg.preserve_tables:
            flush()
            chunks.extend(_table_chunks(text, block, cfg, tier, context, path))
            continue
        if block["block_type"] == "table_row" or (
            block["block_type"] == "code" and cfg.preserve_code_blocks
        ):
            flush()
            start, end = int(block["meta"]["content_start"]), int(block["meta"]["content_end"])
            warnings = (
                ["超大保护块已受控拆分，请核对代码/公式/字段完整性"]
                if end - start > cfg.chunk_size
                else []
            )
            chunks.extend(
                _range_chunks(text, start, end, [block], cfg, tier, context, path, warnings)
            )
            continue
        if pending:
            first = pending[0]
            different_page = first["page_no"] != block["page_no"]
            different_sheet = first["source_locator"].get("sheet") != block["source_locator"].get(
                "sheet"
            )
            candidate = text[
                int(first["meta"]["content_start"]) : int(block["meta"]["content_end"])
            ]
            heading_only = all(b["block_type"] == "heading" for b in pending)
            if (
                different_page
                or different_sheet
                or (not heading_only and not _fits(candidate, pending_context, cfg))
            ):
                flush()
        if not pending:
            pending_context = context
        pending.append(block)
    flush()
    return chunks


def _fits(content: str, context: str, cfg: ChunkingConfig) -> bool:
    value = f"{context}\n\n{content}" if context else content
    return len(value) <= cfg.chunk_size and (
        cfg.token_limit is None or len(value.encode("utf-8")) <= cfg.token_limit
    )


def _window_end(text: str, start: int, end: int, context: str, cfg: ChunkingConfig) -> int:
    available = cfg.chunk_size - (len(context) + 2 if context else 0)
    if available <= 0:
        raise ChunkingError("上下文标题/表头超过字符预算，需提高 chunk_size")
    high = min(end, start + available)
    low = start
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(text[start:middle], context, cfg):
            low = middle
        else:
            high = middle - 1
    if low == start:
        raise ChunkingError("上下文标题/表头超过保守 token 预算，需提高 token_limit")
    return low


def _boundary(
    text: str, start: int, end: int, last_end: int, cfg: ChunkingConfig, tier: str
) -> int:
    floor = max(last_end + 1, start + min(cfg.min_chunk_chars, end - start))
    if tier == "legacy":
        position = text.rfind("\n", floor, end)
        return position if position >= floor else end
    if tier in {"heading", "heuristic"}:
        # Paragraph, then sentence boundaries. Sentence suffixes include punctuation.
        for pattern in (r"\n\n|\n", r"[。！？；]|[!?;](?:\s|$)|\.(?:\s|$)"):
            matches = list(re.finditer(pattern, text[floor:end]))
            if matches:
                return floor + matches[-1].end()
        return end
    for separator in cfg.separators:
        position = text.rfind(separator, floor, end)
        if position >= floor:
            return position + len(separator)
    return end


def _range_chunks(
    text: str,
    start: int,
    end: int,
    blocks: list[DocumentBlock],
    cfg: ChunkingConfig,
    tier: str,
    context: str,
    path: list[str],
    warnings: list[str] | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    cursor = start
    last_end = start - 1
    while cursor < end:
        limit = _window_end(text, cursor, end, context, cfg)
        boundary = end if limit == end else _boundary(text, cursor, limit, last_end, cfg, tier)
        if boundary <= cursor or boundary > limit:
            raise ChunkingError("invalid_boundary_progress")
        content = text[cursor:boundary]
        if not content.strip():
            cursor = boundary
            continue
        hard_split = boundary < end and boundary == limit
        current_warnings = list(warnings or [])
        if hard_split:
            current_warnings.append("硬切边界：没有预算内安全分隔符，语义单元可能跨块")
        chunks.append(
            _make_chunk(text, cursor, boundary, blocks, context, path, current_warnings, hard_split)
        )
        if boundary == end:
            break
        last_end = boundary
        # Overlap cannot cause the same old newline to be selected repeatedly.
        cursor = max(
            cursor + 1, boundary - min(cfg.chunk_overlap, max(0, (boundary - cursor) // 2))
        )
    return chunks


def _make_chunk(
    text: str,
    start: int,
    end: int,
    blocks: list[DocumentBlock],
    context: str,
    path: list[str],
    warnings: list[str],
    hard_split: bool = False,
) -> Chunk:
    touched = [
        b
        for b in blocks
        if int(b["meta"]["content_start"]) < end and int(b["meta"]["content_end"]) > start
    ]
    locators: list[dict[str, Any]] = []
    for block in touched:
        block_start, block_end = int(block["meta"]["content_start"]), int(
            block["meta"]["content_end"]
        )
        locators.append(
            {
                **block["source_locator"],
                "block_id": block["block_id"],
                "block_start": max(start, block_start) - block_start,
                "block_end": min(end, block_end) - block_start,
            }
        )
    pages = {b["page_no"] for b in touched}
    return Chunk(
        content=text[start:end],
        start=start,
        end=end,
        context_header=context,
        section_path=list(path),
        heading_level=touched[-1]["meta"].get("heading_level") if touched else None,
        block_ids=[b["block_id"] for b in touched],
        source_locator=locators,
        page_no=next(iter(pages)) if len(pages) == 1 else None,
        warning=warnings,
        hard_split=hard_split,
    )


def _table_chunks(
    text: str,
    block: DocumentBlock,
    cfg: ChunkingConfig,
    tier: str,
    context: str,
    path: list[str],
) -> list[Chunk]:
    start, end = int(block["meta"]["content_start"]), int(block["meta"]["content_end"])
    lines = block["text"].splitlines(keepends=True)
    table_id = str(block["meta"].get("table_id", block["block_id"]))
    if len(lines) < 2 or not tables.is_separator(lines[1]):
        result = _range_chunks(
            text,
            start,
            end,
            [block],
            cfg,
            tier,
            context,
            path,
            ["表格结构无法识别，原始内容保留但未补表头"],
        )
        for chunk in result:
            chunk.table_id = table_id
        return result
    header_end = start + len(lines[0]) + len(lines[1])
    header = text[start:header_end].rstrip("\n")
    augmented = "\n\n".join(value for value in (context, header) if value)
    # If a header itself cannot fit, never exceed budgets or silently truncate.
    if not _fits(header, context, cfg) or not _fits("x", augmented, cfg):
        result = _range_chunks(
            text,
            start,
            end,
            [block],
            cfg,
            tier,
            context,
            path,
            ["表头超过预算，无法完整补表头；原始表格保留需人工核对"],
        )
        for chunk in result:
            chunk.table_id = table_id
        return result
    row_bounds = []
    row_start = header_end
    for index, line in enumerate(lines[2:], 1):
        row_bounds.append((index, row_start, row_start + len(line)))
        row_start += len(line)
    if not row_bounds:
        chunk = _make_chunk(text, start, end, [block], context, path, [])
        chunk.table_id = table_id
        return [chunk]
    result = []
    cursor = start
    row_first = 1
    last_row = 0
    for row_index, row_start, row_end in row_bounds:
        current_context = context if cursor == start else augmented
        if _fits(text[cursor:row_end], current_context, cfg):
            last_row = row_index
            continue
        if row_start > cursor:
            chunk = _make_chunk(text, cursor, row_start, [block], current_context, path, [])
            chunk.table_id, chunk.row_range = table_id, (
                [row_first, last_row] if last_row else [0, 0]
            )
            result.append(chunk)
            cursor, row_first = row_start, row_index
        # One row larger than the budget is split with a warning and same row identity.
        if not _fits(text[row_start:row_end], augmented, cfg):
            pieces = _range_chunks(
                text,
                row_start,
                row_end,
                [block],
                cfg,
                tier,
                augmented,
                path,
                ["超长表格行被硬切，列关系需要人工核验"],
            )
            for chunk in pieces:
                chunk.table_id, chunk.row_range = table_id, [row_index, row_index]
            result.extend(pieces)
            cursor, row_first = row_end, row_index + 1
        last_row = row_index
    if cursor < end:
        chunk = _make_chunk(
            text, cursor, end, [block], context if cursor == start else augmented, path, []
        )
        chunk.table_id, chunk.row_range = table_id, [row_first, last_row]
        result.append(chunk)
    return result
