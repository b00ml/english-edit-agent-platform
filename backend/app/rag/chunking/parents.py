"""Build parent spans from compatible P0 child chunks without breaking source coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.rag.chunking import Chunk
from app.rag.document import ParsedDocument


@dataclass
class ParentPlan:
    chunk: Chunk
    child_indexes: list[int]


def parent_plans(document: ParsedDocument, children: list[Chunk]) -> list[ParentPlan]:
    if (
        not settings.RAG_PARENT_CHILD_ENABLED
        or len(document["text"]) < settings.RAG_PARENT_MIN_CHARS
    ):
        return []
    if any(child.source_segments for child in children):
        return _structural_parents(document, children)
    plans: list[ParentPlan] = []
    group: list[int] = []

    def flush() -> None:
        nonlocal group
        if len(group) > 1:
            first, last = children[group[0]], children[group[-1]]
            start, end = first.start, last.end
            locator: list[dict[str, Any]] = []
            block_ids: list[str] = []
            for index in group:
                for value in children[index].source_locator:
                    if value not in locator:
                        locator.append(value)
                for block_id in children[index].block_ids:
                    if block_id not in block_ids:
                        block_ids.append(block_id)
            rows = [row for index in group if (row := children[index].row_range) is not None]
            plans.append(
                ParentPlan(
                    Chunk(
                        content=document["text"][start:end],
                        start=start,
                        end=end,
                        context_header=first.context_header,
                        section_path=first.section_path,
                        heading_level=first.heading_level,
                        block_ids=block_ids,
                        source_locator=locator,
                        table_id=first.table_id,
                        page_no=first.page_no,
                        row_range=(
                            [min(row[0] for row in rows), max(row[1] for row in rows)]
                            if rows
                            else None
                        ),
                        warning=list(
                            dict.fromkeys(w for index in group for w in children[index].warning)
                        ),
                    ),
                    list(group),
                )
            )
        group = []

    for index, child in enumerate(children):
        if group:
            first = children[group[0]]
            compatible = (first.page_no, first.section_path, first.table_id) == (
                child.page_no,
                child.section_path,
                child.table_id,
            )
            if not compatible or child.end - first.start > settings.RAG_PARENT_SIZE:
                flush()
        group.append(index)
    flush()
    return plans


def _structural_parents(document: ParsedDocument, children: list[Chunk]) -> list[ParentPlan]:
    from app.rag.chunking.structural import VERSION
    from app.versioning import hash_value

    plans: list[ParentPlan] = []
    group: list[int] = []

    def flush() -> None:
        nonlocal group
        if len(group) > 1:
            first = children[group[0]]
            bounds: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
            for i in group:
                for segment in children[i].source_segments:
                    bounds.setdefault(segment["block_id"], []).append(
                        (segment["source_start"], segment["source_end"], segment)
                    )
            merged = []
            for values in bounds.values():
                values.sort(key=lambda value: value[0])
                a, b, base = values[0]
                for lower, upper, s in values[1:]:
                    if lower <= b:
                        b = max(b, upper)
                    else:
                        merged.append((a, b, base))
                        a, b, base = lower, upper, s
                merged.append((a, b, base))
            merged.sort(key=lambda value: value[0])
            parts = []
            segments = []
            cursor = 0
            for a, b, base in merged:
                body = document["text"][a:b]
                parts.append(body)
                segments.append(
                    {
                        **base,
                        "source_start": a,
                        "source_end": b,
                        "content_start": cursor,
                        "content_end": cursor + len(body),
                        "content_hash": hash_value(body),
                    }
                )
                cursor += len(body) + 2
            content = "\n\n".join(parts)
            if len(content) + len(first.context_header) + 2 <= settings.RAG_PARENT_SIZE:
                pages = {s["page_no"] for s in segments}
                chunk = Chunk(
                    content=content,
                    start=segments[0]["source_start"],
                    end=segments[-1]["source_end"],
                    context_header=first.context_header,
                    section_path=list(first.section_path),
                    page_no=next(iter(pages)) if len(pages) == 1 else None,
                    block_ids=list(dict.fromkeys(s["block_id"] for s in segments)),
                    source_locator=[s["source_locator"] for s in segments],
                    source_segments=segments,
                    logical_unit_id=first.logical_unit_id,
                    chunker_version=VERSION,
                    table_id=first.table_id,
                )
                plans.append(ParentPlan(chunk, list(group)))
        group = []

    for i, child in enumerate(children):
        if group:
            first = children[group[0]]
            if (
                child.logical_unit_id != first.logical_unit_id
                or child.table_id != first.table_id
                or sum(len(children[index].content) + 2 for index in [*group, i])
                + len(first.context_header)
                > settings.RAG_PARENT_SIZE
            ):
                flush()
        group.append(i)
    flush()
    return plans
