"""Pure STR-1 reconstruction. Physical blocks/offsets are evidence, never rewritten."""

from __future__ import annotations

import copy
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.rag.document import ParsedDocument, text_hash
from app.rag.structure.references import reference_edges
from app.rag.structure.tables import table_extensions
from app.rag.tables import is_separator
from app.versioning import hash_value

STRUCTURE_VERSION = "rag-structure-v2"
POLICY_PATH = Path(__file__).with_name("policy.yml")


class RoleRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(min_length=1, max_length=32)
    pattern: str = Field(min_length=1, max_length=256)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    furniture_types: list[str]
    native_roles: dict[str, str]
    chapter_pattern: str
    section_pattern: str
    subsection_pattern: str
    roles: list[RoleRule] = Field(max_length=32)
    unit_break_roles: list[str]
    table_reference_pattern: str
    table_bottom_min: float = Field(ge=0, le=1)
    table_top_max: float = Field(ge=0, le=1)
    table_alignment_tolerance: float = Field(gt=0, le=0.2)
    table_continuation_pattern: str
    table_number_pattern: str
    explicit_reference_pattern: str


@lru_cache(maxsize=8)
def _parse_policy(text: str) -> Policy:
    policy = Policy.model_validate(yaml.safe_load(text))
    for pattern in [
        policy.chapter_pattern,
        policy.section_pattern,
        policy.subsection_pattern,
        policy.table_reference_pattern,
        policy.table_continuation_pattern,
        policy.table_number_pattern,
        policy.explicit_reference_pattern,
        *(r.pattern for r in policy.roles),
    ]:
        re.compile(pattern, re.IGNORECASE)
    return policy


def policy() -> tuple[Policy, str]:
    text = POLICY_PATH.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("Structure policy exceeds size limit")
    return _parse_policy(text), hash_value(text)


def _chapter_key(title: str, pattern: str) -> str | None:
    match = re.match(pattern, title)
    if not match:
        return None
    marker = match.group(0)
    number = marker.removeprefix("第").removesuffix("章")
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if number.isdecimal():
        return str(int(number))
    if number and all(char in digits for char in number):
        return str(int("".join(str(digits[char]) for char in number)))
    total = current = 0
    for char in number:
        if char in digits:
            current = digits[char]
        elif char in {"十", "百", "千"}:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        else:
            return marker
    return str(total + current)


def build_structure(
    document: ParsedDocument,
    *,
    accepted_edge_ids: list[str] | None = None,
    rejected_edge_ids: list[str] | None = None,
    max_blocks: int = 10000,
) -> dict[str, Any]:
    """Build derived navigation/relations; retain every physical source segment.

    Adjacent strong-section body blocks join automatically. Unknown sections/table
    boundaries are proposals, never implicit facts. Gaps/exclusions/new sections
    and question/answer boundaries are not candidates for override.
    """
    document = copy.deepcopy(document)
    document["source_hash"] = document["source_hash"] or text_hash(
        document["original_text"] or document["text"]
    )
    rules, policy_hash = policy()
    source_hash = document["source_hash"] or text_hash(
        document["original_text"] or document["text"]
    )
    blocks = document["blocks"]
    if len(blocks) > max_blocks:
        raise ValueError("Structure block limit exceeded")
    ids = [block["block_id"] for block in blocks]
    by_id = {block["block_id"]: block for block in blocks}
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Structure requires unique source block IDs")
    accepted, rejected = set(accepted_edge_ids or []), set(rejected_edge_ids or [])
    if accepted & rejected or len(accepted) + len(rejected) > 256:
        raise ValueError("Invalid/conflicting structure decisions")
    info: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    units: dict[str, dict[str, Any]] = {}
    barriers: list[dict[str, Any]] = []
    headings: list[tuple[int, str, str]] = []
    previous: str | None = None
    previous_page: int | None = None
    previous_sheet: str | None = None
    previous_order: int | None = None
    active_unit: str | None = None
    unit_section: str | None = None
    known_chapter: str | None = None

    def identity(kind: str, key: str) -> str:
        return hash_value([STRUCTURE_VERSION, source_hash, kind, key])[:32]

    def barrier(block_id: str, reason: str) -> None:
        nonlocal previous, active_unit, unit_section
        barriers.append({"block_id": block_id, "reason": reason})
        previous, active_unit, unit_section = None, None, None

    for block in blocks:
        bid, text, meta = block["block_id"], block["text"], block["meta"]
        start, end = meta.get("content_start"), meta.get("content_end")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or document["text"][start:end] != text
        ):
            raise ValueError("Source offsets/content do not match normalized snapshot")
        page, order = block["page_no"], block["order"]
        sheet = block["source_locator"].get("sheet")
        if previous_sheet is not None and sheet is not None and sheet != previous_sheet:
            barrier(bid, "different_sheet")
            headings, known_chapter = [], None
        if sheet is not None:
            previous_sheet = str(sheet)
        if previous_order is not None and order <= previous_order:
            raise ValueError("Source block order is not increasing")
        if previous_order is not None and order != previous_order + 1:
            barrier(bid, "excluded_or_missing_block")
        previous_order = order
        if meta.get("reset_section_context") or (
            page is not None
            and previous_page is not None
            and page not in {previous_page, previous_page + 1}
        ):
            barrier(bid, "non_contiguous_pages")
            headings, known_chapter = [], None
        if page is not None:
            previous_page = page
        furniture = (
            meta.get("native_type") in rules.furniture_types
            or meta.get("chapter_header_candidate") is True
        )
        role = (
            "furniture"
            if furniture
            else (
                "table"
                if block["block_type"] in {"table", "table_row"}
                else "code" if block["block_type"] == "code" else "body"
            )
        )
        role = rules.native_roles.get(str(meta.get("native_type")), role) if not furniture else role
        role_text = (
            str(meta.get("title") or re.sub(r"^#+\s*", "", text)).strip()
            if block["block_type"] == "heading"
            else text.strip()
        )
        if not furniture:
            for rule in rules.roles:
                if re.search(rule.pattern, role_text, re.IGNORECASE):
                    role = rule.role
                    break
        path = [heading[1] for heading in headings]
        item: dict[str, Any] = {
            "block_id": bid,
            "page_no": page,
            "content_start": start,
            "content_end": end,
            "content_hash": hash_value(text),
            "source_locator": dict(block["source_locator"]),
            "role": role,
            "physical_type": block["block_type"],
            "section_path": path,
            "section_id": headings[-1][2] if headings else None,
            "unit_id": None,
            "table_id": meta.get("table_id"),
            "related_table_id": meta.get("related_table_id"),
        }
        if block["block_type"] == "table":
            lines = text.splitlines(keepends=True)
            if len(lines) > max_blocks:
                raise ValueError("Table provenance row limit exceeded")
            recognized = len(lines) > 1 and is_separator(lines[1])
            line_start = start
            spans = []
            for index, line in enumerate(lines):
                spans.append(
                    {
                        "start": line_start,
                        "end": line_start + len(line),
                        "header": recognized and index < 2,
                        "row": (
                            (0 if index == 0 else None if index == 1 else index - 1)
                            if recognized
                            else None
                        ),
                    }
                )
                line_start += len(line)
            item["table_line_spans"] = spans
            item["table_header_recognized"] = recognized
        info[bid] = item
        if furniture:
            indicated = _chapter_key(text.strip(), rules.chapter_pattern)
            if indicated is not None and known_chapter is not None and indicated != known_chapter:
                barrier(bid, "header_indicates_different_chapter")
                headings, known_chapter = [], None
                item.update(section_path=[], section_id=None)
            # Same-chapter running headers never reset the active subsection.
            item["reason"] = "layout_furniture_not_body_heading"
            continue
        if block["block_type"] == "page_break":
            if not meta.get("reset_section_context"):
                barrier(bid, "empty_or_unparsed_page")
                headings, known_chapter = [], None
            continue
        if not text.strip():
            barrier(bid, "empty_source")
            continue
        if (
            block["block_type"] != "heading"
            and role == "body"
            and "\n" not in text.strip()
            and len(text.strip()) <= 128
            and re.match(rules.chapter_pattern, text.strip())
        ):
            barrier(bid, "unverified_chapter_boundary")
            headings, known_chapter = [], None
            item.update(section_path=[], section_id=None, reason="untyped_chapter_candidate")
        structural_heading = block["block_type"] == "heading" and role == "body"
        if structural_heading:
            title = str(meta.get("title") or re.sub(r"^#+\s*", "", text)).strip()
            if re.match(rules.chapter_pattern, title):
                level = 1
                known_chapter = _chapter_key(title, rules.chapter_pattern)
            elif re.match(rules.section_pattern, title):
                level = 2
            elif re.match(rules.subsection_pattern, title):
                level = 3
            else:
                level = block["level"] or 2
            barrier(bid, "new_chapter" if level == 1 else "new_structural_section")
            while headings and headings[-1][0] >= level:
                headings.pop()
            sid = identity("section", bid)
            parent = headings[-1][2] if headings else None
            headings.append((level, title, sid))
            sections.append(
                {
                    "id": sid,
                    "parent_id": parent,
                    "level": level,
                    "title": title,
                    "source_block_id": bid,
                    "chapter": known_chapter,
                }
            )
            item.update(role="heading", section_path=[h[1] for h in headings], section_id=sid)
        if role in rules.unit_break_roles:
            barrier(bid, "independent_question_or_answer")
        section = item["section_id"]
        prev = info.get(previous or "")
        strong = section is not None
        table_join = False
        if prev and (role == "table" or prev["role"] == "table"):
            native_related = meta.get("related_table_id")
            prev_block = by_id.get(previous or "")
            table_join = bool(
                (native_related and native_related == prev.get("table_id"))
                or (item.get("table_id") and item["table_id"] == prev.get("related_table_id"))
                or (
                    role == "table"
                    and prev_block
                    and re.search(rules.table_reference_pattern, prev_block["text"], re.IGNORECASE)
                )
            )
        joins = bool(
            prev
            and strong
            and prev["section_id"] == section
            and role not in rules.unit_break_roles
            and prev["role"] not in rules.unit_break_roles
            and (role != "table" and prev["role"] != "table" or table_join)
        )
        # A complete source table is one logical unit, not a page-sized parent.
        if active_unit is None or unit_section != section or not joins:
            active_unit = identity("unit", bid)
            unit_section = section
            units[active_unit] = {
                "id": active_unit,
                "section_id": section,
                "section_path": list(item["section_path"]),
                "member_ids": [],
            }
        item["unit_id"] = active_unit
        units[active_unit]["member_ids"].append(bid)
        if (
            prev
            and prev["section_id"] == section
            and role not in rules.unit_break_roles
            and prev["role"] not in rules.unit_break_roles
        ):
            relation = (
                "table_annotation"
                if table_join
                and (role in {"caption", "footnote"} or prev["role"] in {"caption", "footnote"})
                else (
                    "definition_to_table"
                    if table_join
                    else "same_knowledge_unit" if joins else "adjacent_context"
                )
            )
            edge_id = identity("edge", str(prev["block_id"]) + ":" + bid + ":" + relation)
            state = "accepted" if joins else "proposed"
            evidence = ["shared_structural_section"] if joins else ["physical_adjacency_only"]
            if table_join:
                native_connection = bool(
                    meta.get("related_table_id") or prev.get("related_table_id")
                )
                evidence.append(
                    "native_table_relationship" if native_connection else "explicit_table_reference"
                )
            if page != prev["page_no"]:
                evidence.append("continuous_physical_pages")
            if edge_id in accepted:
                state, evidence = "accepted", [*evidence, "reviewer_confirmed"]
            if edge_id in rejected:
                state, evidence = "rejected", [*evidence, "reviewer_rejected"]
            edges.append(
                {
                    "id": edge_id,
                    "from": previous,
                    "to": bid,
                    "relation": relation,
                    "state": state,
                    "evidence": evidence,
                }
            )
        previous = bid
    extra, logical_tables = table_extensions(document, info, barriers, accepted, rejected, rules)
    references, unresolved_references = reference_edges(
        document, info, sections, accepted, rejected, rules
    )
    edges.extend(extra)
    edges.extend(references)
    edge_ids = {edge["id"] for edge in edges}
    if (accepted | rejected) - edge_ids:
        raise ValueError("Structure decisions refer to unknown/barrier-crossing edges")
    signature = hash_value(
        {
            "version": STRUCTURE_VERSION,
            "policy_hash": policy_hash,
            "source_hash": source_hash,
            "parser_version": document["parser_version"],
            "source_text_hash": hash_value(document["text"]),
            "blocks": [
                {**item, "source_locator": item["source_locator"]} for item in info.values()
            ],
            "edges": edges,
            "logical_tables": logical_tables,
            "unresolved_references": unresolved_references,
            "extension_contract_version": "str34-table-reference-v1",
            "max_blocks": max_blocks,
        }
    )
    return {
        "version": STRUCTURE_VERSION,
        "policy_hash": policy_hash,
        "signature": signature,
        "source_hash": source_hash,
        "sections": sections,
        "units": list(units.values()),
        "blocks": list(info.values()),
        "edges": edges,
        "barriers": barriers,
        "logical_tables": logical_tables,
        "unresolved_references": unresolved_references,
        "decisions": {"accepted_edge_ids": sorted(accepted), "rejected_edge_ids": sorted(rejected)},
        "counts": {
            "sections": len(sections),
            "units": len(units),
            "source_segments": len(info),
            "accepted_edges": sum(e["state"] == "accepted" for e in edges),
            "proposed_edges": sum(e["state"] == "proposed" for e in edges),
            "furniture": sum(i["role"] == "furniture" for i in info.values()),
        },
    }
