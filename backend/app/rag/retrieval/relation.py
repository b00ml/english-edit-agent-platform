"""STR-2: active-leaf-only, relation-aware bundles with exact delivered source segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag.knowledge_points import KnowledgeScope
from app.rag.retrieval.context import citation
from app.rag.retrieval.models import Candidate, chunk_metadata, leaf_predicate, predicates
from app.rag.retrieval.table_context import logical_views
from app.rag.structure.runtime import active_rows, derive
from app.versioning import hash_value

PROTOCOL_VERSION = "rag-context-v2"


@dataclass
class Part:
    row: KnowledgeChunk
    block: dict[str, Any] | None
    start: int
    end: int
    table_header: bool = False
    table_row: int | None = None
    partial_table_line: bool = False
    content_origin: int | None = None

    @property
    def content(self) -> str:
        origin = (
            self.content_origin
            if self.content_origin is not None
            else self.row.content_start if self.block is not None else 0
        )
        return self.row.content[self.start - cast(int, origin) : self.end - cast(int, origin)]


@dataclass
class DocumentState:
    plan: dict[str, Any]
    parts: dict[str, list[Part]]
    graph: dict[str, list[str]]
    blocked: bool = False
    reason: str | None = None


def _load(
    session: Session,
    seed: KnowledgeChunk,
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
) -> DocumentState | None:
    document = session.scalar(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.id == seed.document_id,
            KnowledgeDocument.tenant_id == seed.tenant_id,
        )
        .with_for_update(read=True)
    )
    if document is None or not document.blocks or not settings.RAG_STRUCTURE_ENABLED:
        return None
    all_rows = active_rows(session, document)
    plan = derive(document, all_rows)
    review = (document.meta or {}).get("structure_review", {})
    indexed = (document.meta or {}).get("structure", {})
    reason = (
        "stale_index_no_expansion"
        if document.status != "indexed"
        else (
            "structure_review_stale"
            if review
            and (
                review.get("review_signature") != plan["review_signature"]
                or review.get("source_scope_signature") != plan["source_scope_signature"]
            )
            else (
                "structure_policy_changed"
                if indexed and indexed.get("signature") != plan["signature"] and not review
                else None
            )
        )
    )
    allowed_ids = set(
        session.scalars(
            select(KnowledgeChunk.id)
            .where(
                *predicates(scope, tenant, allow_all, session, [document.id]),
                leaf_predicate(),
                KnowledgeChunk.tenant_id == document.tenant_id,
            )
            .limit(settings.RAG_STRUCTURE_MAX_LEAFS + 1)
        ).all()
    )
    pieces: dict[str, list[Part]] = {block["block_id"]: [] for block in plan["blocks"]}
    blocks = plan["blocks"]

    def add(row: KnowledgeChunk, start: int, end: int, origin: int) -> None:
        for block in blocks:
            if (
                block["role"] == "furniture"
                or block["content_end"] <= start
                or block["content_start"] >= end
            ):
                continue
            lower, upper = max(start, block["content_start"]), min(end, block["content_end"])
            if lower >= upper:
                continue
            if block.get("table_header_recognized"):
                for line in block["table_line_spans"]:
                    lo, hi = max(lower, line["start"]), min(upper, line["end"])
                    if lo < hi:
                        pieces[block["block_id"]].append(
                            Part(
                                row,
                                block,
                                lo,
                                hi,
                                line["header"],
                                line["row"],
                                lo != line["start"] or hi != line["end"],
                                origin,
                            )
                        )
            else:
                pieces[block["block_id"]].append(
                    Part(row, block, lower, upper, content_origin=origin)
                )

    for row in all_rows:
        if row.id not in allowed_ids:
            continue
        segments = chunk_metadata(row).get("source_segments")
        if segments:
            valid = True
            for segment in segments:
                start, end = segment["source_start"], segment["source_end"]
                lo, hi = segment["content_start"], segment["content_end"]
                if document.normalized_text[start:end] != row.content[lo:hi] or segment[
                    "content_hash"
                ] != hash_value(row.content[lo:hi]):
                    valid = False
                    break
            if not valid:
                continue
            for segment in segments:
                add(
                    row,
                    segment["source_start"],
                    segment["source_end"],
                    segment["source_start"] - segment["content_start"],
                )
        elif (
            row.content_start is not None
            and row.content_end is not None
            and document.normalized_text[row.content_start : row.content_end] == row.content
        ):
            add(row, row.content_start, row.content_end, row.content_start)
    graph: dict[str, list[str]] = {block["block_id"]: [] for block in blocks}
    for edge in plan["edges"]:
        if edge["state"] == "accepted" and edge["relation"] != "table_cell_continues":
            graph[edge["from"]].append(edge["to"])
            if edge["relation"] != "references_section":
                graph[edge["to"]].append(edge["from"])
    return DocumentState(plan, pieces, {} if reason else graph, bool(reason), reason)


def _reachable(state: DocumentState, starts: list[str]) -> tuple[list[str], list[str]]:
    seen, pending, reasons = set(starts), [(key, 0) for key in starts], []
    result: list[str] = []
    while pending:
        key, depth = pending.pop(0)
        if len(result) >= settings.RAG_CONTEXT_BUNDLE_MAX_MEMBERS:
            reasons.append("member_limit")
            break
        if not state.parts.get(key):
            reasons.append("scope_or_active_member_missing")
            continue
        result.append(key)
        for target in state.graph.get(key, []):
            if target in seen:
                continue
            seen.add(target)
            if depth >= settings.RAG_CONTEXT_BUNDLE_MAX_HOPS:
                reasons.append("hop_limit")
                continue
            pending.append((target, depth + 1))
    return result, list(dict.fromkeys(reasons))


def _clip(body: str, maximum: int, byte_limit: int | None, protected: bool) -> str:
    if maximum < 1 or byte_limit is not None and byte_limit < 1:
        return ""
    value = body[:maximum]
    if byte_limit is not None:
        value = value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")
    if len(value) == len(body):
        return value
    if protected:
        # No broken cell/row/code fence. Entire source segment must fit.
        return ""
    boundary = max(value.rfind("\n"), value.rfind("。") + 1, value.rfind(". ") + 1)
    return value[:boundary] if boundary >= 16 else value if len(value) >= 16 else ""


def _segment(part: Part, candidate: Candidate, text: str, bundle_id: str) -> dict[str, Any]:
    row = part.row
    info = citation(row, candidate)
    block = part.block
    partial = len(text) != len(part.content)
    table_id = block.get("table_id") if block else chunk_metadata(row).get("table_id")
    warnings = chunk_metadata(row).get("warnings", [])
    partial_row = bool(
        table_id and (part.partial_table_line or any("硬切" in str(value) for value in warnings))
    )
    locators = (
        [
            {
                **block["source_locator"],
                "block_id": block["block_id"],
                "block_start": part.start - block["content_start"],
                "block_end": part.start - block["content_start"] + len(text),
                "bbox_precision": "source_block_not_clipped_text",
            }
        ]
        if block
        else info["source_locator"]
    )
    info.update(
        bundle_id=bundle_id,
        segment_id=hash_value([row.id, part.start, len(text), hash_value(text)])[:32],
        content=text,
        content_hash=hash_value(text),
        source_content_hash=hash_value(row.content),
        page_no=block["page_no"] if block else row.page_no,
        content_start=part.start if block else row.content_start,
        content_end=(
            part.start + len(text)
            if block
            else (row.content_start + len(text) if row.content_start is not None else None)
        ),
        source_locator=locators,
        block_id=block["block_id"] if block else None,
        logical_section_id=block["section_id"] if block else None,
        logical_unit_id=block["unit_id"] if block else None,
        section_path=block["section_path"] if block else row.section_path or [],
        source_role=block["role"] if block else "unknown",
        table_id=table_id,
        logical_table_id=block.get("logical_table_id") if block else None,
        matched_child_ids=[row.id],
        match_chunk_id=row.id,
        context_role="seed" if row.id == candidate.row.id else "relation_expansion",
        context_header="",
        truncated=partial,
        partial_row=partial_row,
        seed_chunk_id=candidate.row.id,
        source_row_range=info.get("row_range"),
        row_range=[part.table_row, part.table_row] if part.table_row is not None else None,
        table_header=part.table_header,
        normalized_table_row=part.table_row,
    )
    return info


def _render_segment(segment: dict[str, Any]) -> str:
    page = segment["page_no"]
    locator = f" · page {page}" if page is not None else ""
    return f"[来源 {segment['chunk_id']}: {segment['source_name']}{locator}]\n{segment['content']}"


def _groups(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for segment in segments:
        if (
            groups
            and segment.get("table_id")
            and (segment.get("document_id"), segment.get("block_id"), segment.get("table_id"))
            == (
                groups[-1][0].get("document_id"),
                groups[-1][0].get("block_id"),
                groups[-1][0].get("table_id"),
            )
        ):
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def _body(segments: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        (
            "\n".join(segment["content"].rstrip("\n") for segment in group)
            if group[0].get("table_id")
            else group[0]["content"]
        )
        for group in _groups(segments)
    )


def _render(segments: list[dict[str, Any]]) -> str:
    values = []
    for group in _groups(segments):
        if group[0].get("table_id"):
            source = group[0]
            ids = ",".join(dict.fromkeys(segment["chunk_id"] for segment in group))
            marker = _render_segment({**source, "chunk_id": ids, "content": ""}).rstrip("\n")
            values.append(
                marker + "\n" + "\n".join(segment["content"].rstrip("\n") for segment in group)
            )
        else:
            values.append(_render_segment(group[0]))
    return "\n\n".join(values)


def expand_relations(
    session: Session,
    candidates: list[Candidate],
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
    top_k: int,
    diagnostics: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return <=top_k bundles with <=configured total segments; old zipped fields stay valid."""
    cache: dict[str, DocumentState | None] = {}
    selected_components: set[tuple[str | None, tuple[str, ...]]] = set()
    occupied: dict[str, list[tuple[int, int]]] = {}
    texts: list[str] = []
    citations: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    used_chars = used_bytes = segment_count = seed_count = 0
    for candidate in candidates:
        if len(bundles) >= top_k:
            break
        row = candidate.row
        reasons: list[str] = []
        state = None
        if row.document_id:
            if row.document_id not in cache:
                try:
                    with session.begin_nested():
                        cache[row.document_id] = _load(session, row, scope, tenant, allow_all)
                except (ValueError, KeyError, TypeError, SQLAlchemyError) as exc:
                    cache[row.document_id] = None
                    diagnostics.setdefault("fallbacks", []).append(
                        {"stage": "structure", "error_type": type(exc).__name__}
                    )
            state = cache[row.document_id]
        if state and not state.blocked:
            starts = [
                key
                for key, parts in state.parts.items()
                if any(part.row.id == row.id for part in parts)
            ]
            if not starts:
                continue  # Pure furniture or unsafe/mismatched source coordinates are not evidence.
            weights: dict[str, float] = {}
            body_units: set[str] = set()
            for key in starts:
                for piece in state.parts[key]:
                    if piece.row.id != row.id or not piece.block:
                        continue
                    unit = str(piece.block["unit_id"])
                    weights[unit] = weights.get(unit, 0) + (piece.end - piece.start) * (
                        0.25 if piece.block["role"] == "heading" else 1
                    )
                    if piece.block["role"] != "heading":
                        body_units.add(unit)
            primary = max(weights, key=lambda key: weights[key]) if weights else None
            starts = [
                key
                for key in starts
                if any(
                    str(piece.block["unit_id"]) == primary
                    for piece in state.parts[key]
                    if piece.row.id == row.id and piece.block
                )
            ]
            reachable, reasons = _reachable(state, starts)
            if len(body_units) > 1:
                reasons.append("seed_spans_multiple_logical_units")
            component = (row.document_id, tuple(sorted(reachable)))
            if component in selected_components:
                continue
            members = [part for key in reachable for part in state.parts.get(key, [])]
            if not any(part.block and part.block["role"] != "heading" for part in members):
                continue  # A navigation/title-only unit is not a usable knowledge source.
            # Seed pieces before expanded context: large preceding prose cannot evict the hit.
            members.sort(
                key=lambda part: (
                    0 if part.table_header else 1 if part.row.id == row.id else 2,
                    part.start,
                    part.end,
                    part.row.id,
                )
            )
        else:
            reasons = (
                [state.reason or "structure_unavailable"]
                if state
                else ["legacy_source_no_structure"]
            )
            if state and chunk_metadata(row).get("source_segments"):
                members = [
                    part
                    for parts in state.parts.values()
                    for part in parts
                    if part.row.id == row.id
                ]
                if not members:
                    continue
            elif chunk_metadata(row).get("source_segments"):
                # Missing/invalid structure cannot invent a page/range for a composite source.
                diagnostics.setdefault("context_rejections", []).append(
                    "composite_source_unvalidated"
                )
                continue
            else:
                members = [Part(row, None, 0, len(row.content))]
            component = (row.document_id, (row.id,))
        seed_count += 1
        bundle_id = hash_value([PROTOCOL_VERSION, row.document_id, component[1], row.id])[:32]
        section: list[str] = next(
            (p.block["section_path"] for p in members if p.block and p.row.id == row.id), []
        )
        prefix = "知识单元：" + " > ".join(section) if section else "知识来源片段"
        prefix = prefix[: min(256, settings.RAG_CONTEXT_MAX_CHARS // 4)]
        segments: list[dict[str, Any]] = []
        local_ranges: dict[str, list[tuple[int, int]]] = {}
        local_chars, local_bytes = len(prefix) + 2, len(prefix.encode("utf-8")) + 2
        remaining_parts = max(
            1, len(diagnostics.get("question_plan", {}).get("parts", [])) - len(bundles)
        )
        fair_chars = (settings.RAG_CONTEXT_MAX_CHARS - used_chars) // remaining_parts
        fair_bytes = (
            (settings.RAG_CONTEXT_TOKEN_LIMIT - used_bytes) // remaining_parts
            if settings.RAG_CONTEXT_TOKEN_LIMIT is not None
            else None
        )
        for part in members:
            if segment_count + len(segments) >= settings.RAG_CONTEXT_MAX_SEGMENTS:
                reasons.append("segment_limit")
                break
            ranges = [(part.start, part.end)]
            # Remove overlap by document offsets, never by semantically similar text.
            key = row.document_id or part.row.id
            for lower, upper in [*occupied.get(key, []), *local_ranges.get(key, [])]:
                ranges = [
                    (a, b)
                    for start, end in ranges
                    for a, b in [(start, min(end, lower)), (max(start, upper), end)]
                    if a < b
                ]
            for lower, upper in ranges:
                if segment_count + len(segments) >= settings.RAG_CONTEXT_MAX_SEGMENTS:
                    reasons.append("segment_limit")
                    break
                piece = Part(
                    part.row,
                    part.block,
                    lower,
                    upper,
                    part.table_header,
                    part.table_row,
                    part.partial_table_line
                    or bool(
                        part.block
                        and part.block["physical_type"] == "table"
                        and (lower != part.start or upper != part.end)
                    ),
                    part.content_origin,
                )
                original = piece.content
                if not original.strip():
                    continue
                temporary = _segment(piece, candidate, "", bundle_id)
                overhead = len(_render_segment(temporary)) + (2 if segments else 0)
                byte_overhead = len(_render_segment(temporary).encode("utf-8")) + (
                    2 if segments else 0
                )
                available = (
                    settings.RAG_CONTEXT_MAX_CHARS
                    - used_chars
                    - local_chars
                    - overhead
                    - (2 if texts else 0)
                )
                byte_limit = (
                    settings.RAG_CONTEXT_TOKEN_LIMIT
                    - used_bytes
                    - local_bytes
                    - byte_overhead
                    - (2 if texts else 0)
                    if settings.RAG_CONTEXT_TOKEN_LIMIT is not None
                    else None
                )
                available = min(available, fair_chars - local_chars - overhead)
                if fair_bytes is not None:
                    byte_limit = min(
                        byte_limit if byte_limit is not None else fair_bytes,
                        fair_bytes - local_bytes - byte_overhead,
                    )
                protected = bool(
                    piece.block and piece.block["physical_type"] in {"table", "table_row", "code"}
                ) or bool(chunk_metadata(piece.row).get("table_id"))
                body = _clip(original, available, byte_limit, protected)
                if not body:
                    reasons.append(
                        "budget_protected_source_skipped" if protected else "budget_source_skipped"
                    )
                    continue
                segment = _segment(piece, candidate, body, bundle_id)
                if len(body) != len(original):
                    reasons.append("budget_truncated")
                if segment["partial_row"]:
                    reasons.append("partial_table_row")
                rendered = _render_segment(segment)
                local_chars += len(rendered) + (2 if segments else 0)
                local_bytes += len(rendered.encode("utf-8")) + (2 if segments else 0)
                segment.update(
                    source_offset_unit="unicode_codepoints",
                    source_snapshot_version=piece.row.parser_version,
                    structure_version=state.plan["version"] if state else None,
                    structure_signature=state.plan["signature"] if state else None,
                    source_scope_signature=state.plan["source_scope_signature"] if state else None,
                    index_revision=state.plan["index_revision"] if state else None,
                )
                segments.append(segment)
                if piece.block:
                    local_ranges.setdefault(key, []).append((lower, lower + len(body)))
        if not segments or not any(segment["chunk_id"] == row.id for segment in segments):
            continue
        if all(s["source_role"] in {"heading", "furniture"} for s in segments):
            diagnostics.setdefault("context_rejections", []).append("only_navigation_delivered")
            continue
        table_blocks = {s.get("block_id") for s in segments if s.get("table_id")}
        missing_headers = [
            block_id
            for block_id in table_blocks
            if state
            and any(
                b["block_id"] == block_id and b.get("table_header_recognized")
                for b in state.plan["blocks"]
            )
            and not any(s.get("block_id") == block_id and s.get("table_header") for s in segments)
        ]
        if missing_headers:
            diagnostics.setdefault("context_rejections", []).append("table_header_not_delivered")
            continue
        # Publication order follows the source, even though seeds received first budget priority.
        segments.sort(
            key=lambda value: (
                value["content_start"] if value["content_start"] is not None else 0,
                value["segment_id"],
            )
        )
        rendered = prefix + "\n\n" + _render(segments)
        if used_chars + len(rendered) + (2 if texts else 0) > settings.RAG_CONTEXT_MAX_CHARS or (
            settings.RAG_CONTEXT_TOKEN_LIMIT is not None
            and used_bytes + len(rendered.encode("utf-8")) + (2 if texts else 0)
            > settings.RAG_CONTEXT_TOKEN_LIMIT
        ):
            diagnostics.setdefault("context_rejections", []).append("render_budget_exceeded")
            continue
        used_chars += len(rendered) + (2 if texts else 0)
        used_bytes += len(rendered.encode("utf-8")) + (2 if texts else 0)
        segment_count += len(segments)
        selected_components.add(component)
        for key, values in local_ranges.items():
            occupied.setdefault(key, []).extend(values)
        pages = sorted({s["page_no"] for s in segments if s["page_no"] is not None})
        body = _body(segments)
        base = citation(row, candidate)
        base.update(
            protocol_version=PROTOCOL_VERSION,
            bundle_id=bundle_id,
            content=body,
            content_hash=hash_value(body),
            content_start=None,
            content_end=None,
            page_no=pages[0] if len(pages) == 1 else None,
            pages=pages,
            context_header=prefix,
            section_path=section,
            context_role="bundle",
            source_segments=segments,
            matched_child_ids=list(dict.fromkeys(s["chunk_id"] for s in segments)),
            truncated=any(s["truncated"] for s in segments),
            complete=not reasons,
            incomplete_reasons=list(dict.fromkeys(reasons)),
        )
        table_views = logical_views(segments, state.plan.get("logical_tables", [])) if state else []
        bundle = {
            "id": bundle_id,
            "seed_chunk_id": row.id,
            "document_id": row.document_id,
            "structure_version": state.plan["version"] if state else None,
            "structure_signature": state.plan["signature"] if state else None,
            "source_scope_signature": state.plan["source_scope_signature"] if state else None,
            "pages": pages,
            "section_path": section,
            "source_segments": segments,
            "complete": not reasons,
            "incomplete_reasons": list(dict.fromkeys(reasons)),
            "rendered_context": rendered,
            "logical_table_views": table_views,
            "reference_edges": (
                [
                    e
                    for e in state.plan["edges"]
                    if e["relation"] == "references_section" and e["state"] == "accepted"
                ]
                if state
                else []
            ),
        }
        additions = "\n\n".join(
            "[结构化表格视图；确认续接，原始来源段保留]\n" + view["rendered_table"]
            for view in table_views
            if len(view["physical_table_ids"]) > 1
        )
        for view in table_views:
            view["context_delivered"] = len(view["physical_table_ids"]) == 1
        if additions:
            extra = "\n\n" + additions
            if used_chars + len(extra) <= settings.RAG_CONTEXT_MAX_CHARS and (
                settings.RAG_CONTEXT_TOKEN_LIMIT is None
                or used_bytes + len(extra.encode("utf-8")) <= settings.RAG_CONTEXT_TOKEN_LIMIT
            ):
                rendered += extra
                used_chars += len(extra)
                used_bytes += len(extra.encode("utf-8"))
                bundle["rendered_context"] = rendered
                for view in table_views:
                    view["context_delivered"] = True
            else:
                bundle["logical_view_omitted_for_budget"] = True
        texts.append(rendered)
        citations.append(base)
        bundles.append(bundle)
    diagnostics.update(
        protocol_version=PROTOCOL_VERSION,
        context_mode="relation",
        context_bundles=bundles,
        seed_count=seed_count,
        bundle_count=len(bundles),
        segment_count=segment_count,
        context_chars=used_chars,
        context_utf8_bytes=used_bytes,
        completeness_scope="accepted structure materialization, not answer factual correctness",
    )
    return texts, citations
