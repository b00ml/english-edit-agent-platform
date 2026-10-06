"""Logical-table derived views from delivered leaf text, never snapshot grid text."""

from __future__ import annotations

from typing import Any

from app.rag.tables import cells, render
from app.versioning import hash_value


def logical_views(
    segments: list[dict[str, Any]], plans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for plan in plans:
        blocks = {t["block_id"]: t for t in plan["physical_tables"]}
        present = [s for s in segments if s.get("block_id") in blocks and s.get("table_id")]
        if not present:
            continue
        rows: list[dict[str, Any]] = []
        header_values = None
        header_refs = []
        row_map: dict[tuple[str, int], dict[str, Any]] = {}
        for s in present:
            table = blocks[s["block_id"]]
            line = s["content"].strip()
            if s.get("normalized_table_row") is None:
                continue
            values = cells(line)
            if not values or s.get("partial_row") or s.get("truncated"):
                continue
            nr = s["normalized_table_row"]
            source_row = (
                table["normalized_row_to_source"][nr - 1]
                if nr > 0 and nr - 1 < len(table["normalized_row_to_source"])
                else 0
            )
            reference = {
                "segment_id": s["segment_id"],
                "chunk_id": s["chunk_id"],
                "page_no": s["page_no"],
                "block_id": s["block_id"],
                "source_row": source_row,
            }
            if nr == 0 and table["header_confirmed"]:
                if header_values is None:
                    header_values = values
                    header_refs.append(reference)
                elif values == header_values:
                    header_refs.append(reference)
                else:
                    rows.append(
                        {
                            "cells": [
                                {"text": value, "parts": [{**reference, "column": c}]}
                                for c, value in enumerate(values)
                            ],
                            "different_header": True,
                        }
                    )
                continue
            row: dict[str, Any] = {
                "cells": [
                    {"text": value, "parts": [{**reference, "column": c}]}
                    for c, value in enumerate(values)
                ]
            }
            rows.append(row)
            row_map[(s["block_id"], source_row)] = row
        joins = []
        owners: dict[tuple[str, int, int], dict[str, Any]] = {}
        for edge in plan["cell_edges"]:
            if edge["state"] != "accepted":
                continue
            a = row_map.get((edge["from"], edge["row_a"]))
            b = row_map.get((edge["to"], edge["row_b"]))
            if not a or not b:
                continue
            lo, hi = edge["columns"]
            if hi > len(a["cells"]) or hi > len(b["cells"]):
                continue
            for col in range(lo, hi):
                key_a = (edge["from"], edge["row_a"], col)
                key_b = (edge["to"], edge["row_b"], col)
                ca, cb = owners.get(key_a, a["cells"][col]), b["cells"][col]
                owners[key_b] = ca
                ca.update(
                    text=ca["text"] + " " + cb["text"],
                    parts=[*ca["parts"], *cb["parts"]],
                    derived_join=True,
                )
                cb.update(text="", parts=[], continued_into_previous=True)
            joins.append(edge["id"])
        rows = [r for r in rows if any(c["text"] for c in r["cells"])]
        width = max([len(header_values or []), *(len(r["cells"]) for r in rows)], default=0)
        if not width:
            continue
        display_header = header_values or ["列" + str(i + 1) for i in range(width)]
        grid: list[list[str]] = [display_header, *[[c["text"] for c in r["cells"]] for r in rows]]
        result.append(
            {
                "logical_table_id": plan["id"],
                "physical_table_ids": [t["table_id"] for t in plan["physical_tables"]],
                "header": header_values,
                "header_sources": header_refs,
                "header_synthetic": header_values is None,
                "rows": rows,
                "confirmed_cell_joins": joins,
                "rendered_table": render(grid),
                "derived_content_hash": hash_value(render(grid)),
                "source_segment_ids": [s["segment_id"] for s in present],
                "derived_view": True,
                "physical_provenance": [
                    {
                        key: table.get(key)
                        for key in (
                            "table_id",
                            "block_id",
                            "page_no",
                            "spans",
                            "normalized_row_to_source",
                            "header_rows",
                        )
                    }
                    for table in plan["physical_tables"]
                ],
                "complete_physical_segments": all(
                    all(
                        any(
                            s.get("block_id") == t["block_id"]
                            and not s.get("partial_row")
                            and s.get("content_start") <= line["start"]
                            and s.get("content_end") >= line["end"]
                            for s in present
                        )
                        for line in t["line_spans"]
                    )
                    for t in plan["physical_tables"]
                ),
            }
        )
    return result
