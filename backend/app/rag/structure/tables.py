"""STR-3 table continuity plans. Original grids/spans/text never change."""

from __future__ import annotations

import re
from typing import Any

from app.rag.document import ParsedDocument
from app.rag.tables import cells
from app.versioning import hash_value


def table_extensions(
    document: ParsedDocument,
    info: dict[str, dict[str, Any]],
    barriers: list[dict[str, Any]],
    accepted: set[str],
    rejected: set[str],
    rules: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    originals: dict[str, Any] = {b["block_id"]: b for b in document["blocks"]}
    physical: list[dict[str, Any]] = []
    for b in document["blocks"]:
        if b["block_type"] != "table":
            continue
        item = info[b["block_id"]]
        meta = b["meta"]
        lines = b["text"].splitlines()
        values = meta.get("rows") or [
            cells(line) for i, line in enumerate(lines) if i != 1 and line.strip().startswith("|")
        ]
        spans = meta.get("spans", [])
        header_known = (
            not meta.get("native_type") and item.get("table_header_recognized")
        ) or bool(spans and any(s.get("header") for s in spans))
        related = [
            x
            for x in document["blocks"]
            if x["meta"].get("related_table_id") == meta.get("table_id")
            and x["meta"].get("native_type") == "table_caption"
        ]
        caption = meta.get("caption") or " ".join(x["text"] for x in related)
        bbox = b["source_locator"].get("bbox")
        if len(physical) >= 256 or len(values) > 10000 or len(spans) > 10000:
            raise ValueError("Logical table analysis limit exceeded")
        physical.append(
            {
                "block_id": b["block_id"],
                "table_id": item["table_id"],
                "page_no": b["page_no"],
                "section_id": item["section_id"],
                "rows": values,
                "spans": spans,
                "header_rows": int(meta.get("header_rows", 1)),
                "header_confirmed": bool(header_known),
                "normalized_row_to_source": meta.get(
                    "normalized_row_to_source", list(range(1, len(values)))
                ),
                "caption": caption,
                "bbox": bbox,
                "bbox_units": b["source_locator"].get("bbox_units"),
                "bbox_frame": b["source_locator"].get("bbox_frame"),
                "continues_prev": meta.get("table_continues_prev") is True,
                "line_spans": item.get("table_line_spans", []),
                "content_hash": item["content_hash"],
            }
        )
    edges: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    def eid(a: str, b: str, kind: str, extra: Any = None) -> str:
        return hash_value([document["source_hash"], a, b, kind, extra])[:32]

    def state(key: str, default: str) -> str:
        return "rejected" if key in rejected else "accepted" if key in accepted else default

    for index, current in enumerate(physical):
        gid = hash_value([document["source_hash"], "logical-table", current["block_id"]])[:32]
        group: dict[str, Any] = {
            "id": gid,
            "physical_tables": [current],
            "continuation_edges": [],
            "cell_edges": [],
            "unresolved_cell_boundaries": [],
        }
        if index:
            previous = physical[index - 1]
            a, b = previous["block_id"], current["block_id"]
            between = [
                x
                for x in document["blocks"]
                if originals[a]["order"] < x["order"] < originals[b]["order"]
            ]
            blocked = any(
                x["reason"]
                in {
                    "new_chapter",
                    "new_structural_section",
                    "non_contiguous_pages",
                    "excluded_or_missing_block",
                    "different_sheet",
                    "independent_question_or_answer",
                    "header_indicates_different_chapter",
                    "unverified_chapter_boundary",
                    "empty_or_unparsed_page",
                }
                and originals.get(x["block_id"], {}).get("order", -1) > originals[a]["order"]
                and originals.get(x["block_id"], {}).get("order", -1) <= originals[b]["order"]
                for x in barriers
            )
            bridge_ok = all(
                info[x["block_id"]]["role"] in {"furniture", "caption", "footnote"}
                or x["block_type"] == "page_break"
                for x in between
            )
            consecutive = (
                previous["page_no"] is not None and current["page_no"] == previous["page_no"] + 1
            )
            columns_a = max((len(r) for r in previous["rows"]), default=0)
            columns_b = max((len(r) for r in current["rows"]), default=0)
            if (
                consecutive
                and not blocked
                and bridge_ok
                and columns_a == columns_b
                and columns_a > 0
                and previous["section_id"] == current["section_id"]
            ):
                ba, bb = previous["bbox"], current["bbox"]
                geometry = bool(
                    ba
                    and bb
                    and len(ba) == 4
                    and len(bb) == 4
                    and previous["bbox_units"] == current["bbox_units"] == "normalized_page"
                    and previous["bbox_frame"] is not None
                    and previous["bbox_frame"] == current["bbox_frame"]
                    and ba[3] >= rules.table_bottom_min
                    and bb[1] <= rules.table_top_max
                    and abs(ba[0] - bb[0]) <= rules.table_alignment_tolerance
                    and abs(ba[2] - bb[2]) <= rules.table_alignment_tolerance
                )
                repeated = bool(
                    previous["header_confirmed"]
                    and current["header_confirmed"]
                    and previous["rows"][: previous["header_rows"]]
                    == current["rows"][: current["header_rows"]]
                )
                continuation = bool(
                    re.search(rules.table_continuation_pattern, str(current["caption"]), re.I)
                )
                number_a = re.search(rules.table_number_pattern, str(previous["caption"]), re.I)
                number_b = re.search(rules.table_number_pattern, str(current["caption"]), re.I)
                numbered = bool(
                    number_a
                    and number_b
                    and number_a.group(0).casefold() == number_b.group(0).casefold()
                )
                key = eid(a, b, "table_continues")
                strong = geometry and (
                    (current["continues_prev"] and (repeated or numbered))
                    or (numbered and continuation and repeated)
                )
                evidence = ["adjacent_physical_pages", "compatible_column_count"]
                for flag, label in [
                    (geometry, "aligned_page_bottom_top"),
                    (repeated, "confirmed_repeated_header"),
                    (current["continues_prev"], "native_continuation_hint"),
                    (numbered, "matching_table_number"),
                    (continuation, "caption_continued"),
                ]:
                    if flag:
                        evidence.append(label)
                edge = {
                    "id": key,
                    "from": a,
                    "to": b,
                    "relation": "table_continues",
                    "state": state(key, "accepted" if strong else "proposed"),
                    "evidence": evidence,
                    "repeated_header": repeated,
                }
                if key in accepted:
                    edge["evidence"].append("reviewer_confirmed")
                if key in rejected:
                    edge["evidence"].append("reviewer_rejected")
                edges.append(edge)
                if edge["state"] == "accepted":
                    group = groups[-1]
                    group["physical_tables"].append(current)
                    group["continuation_edges"].append(key)
                # Each cell's evidence/decision is independent. No implicit whole-row concatenation.
                data_b = current["header_rows"] if current["header_confirmed"] else 0
                previous_data = previous["header_rows"] if previous["header_confirmed"] else 0
                if previous_data < len(previous["rows"]) and data_b < len(current["rows"]):
                    row_a = len(previous["rows"]) - 1
                    row_b = data_b
                    ranges_a = [s for s in previous["spans"] if s["row"] == row_a]
                    ranges_b = [s for s in current["spans"] if s["row"] == row_b]
                    if not ranges_a:
                        ranges_a = [
                            {"column": c, "colspan": 1, "rowspan": 1} for c in range(columns_a)
                        ]
                    if not ranges_b:
                        ranges_b = [
                            {"column": c, "colspan": 1, "rowspan": 1} for c in range(columns_b)
                        ]
                    for ca in ranges_a:
                        if len(edges) >= 256:
                            raise ValueError("Table continuation/cell decision limit exceeded")
                        matches = [
                            cb
                            for cb in ranges_b
                            if cb["column"] == ca["column"]
                            and cb.get("colspan", 1) == ca.get("colspan", 1)
                        ]
                        if (
                            len(matches) != 1
                            or ca.get("rowspan", 1) != 1
                            or matches[0].get("rowspan", 1) != 1
                        ):
                            group["unresolved_cell_boundaries"].append(
                                {
                                    "from": a,
                                    "to": b,
                                    "column": ca["column"],
                                    "reason": "span_range_or_rowspan_not_proven",
                                }
                            )
                            continue
                        lo, hi = ca["column"], ca["column"] + ca.get("colspan", 1)
                        ck = eid(a, b, "table_cell_continues", [row_a, row_b, lo, hi])
                        cell = {
                            "id": ck,
                            "from": a,
                            "to": b,
                            "relation": "table_cell_continues",
                            "state": state(ck, "proposed"),
                            "evidence": ["matching_cell_column_range", "boundary_rows_need_review"],
                            "table_edge_id": key,
                            "row_a": row_a,
                            "row_b": row_b,
                            "columns": [lo, hi],
                        }
                        if cell["state"] == "accepted" and edge["state"] != "accepted":
                            raise ValueError(
                                "Cell continuation requires accepted table continuation"
                            )
                        if ck in accepted:
                            cell["evidence"].append("reviewer_confirmed")
                        if ck in rejected:
                            cell["evidence"].append("reviewer_rejected")
                        edges.append(cell)
                        group["cell_edges"].append(cell)
        if not groups or groups[-1] is not group:
            groups.append(group)
    for group in groups:
        for table in group["physical_tables"]:
            item = info[table["block_id"]]
            item["logical_table_id"] = group["id"]
            item["table_provenance"] = {
                k: table[k]
                for k in (
                    "table_id",
                    "header_rows",
                    "header_confirmed",
                    "normalized_row_to_source",
                    "spans",
                )
            }
    return edges, groups
