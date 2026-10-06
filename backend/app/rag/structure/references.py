"""Explicit same-document chapter/section references, never similarity joins."""

from __future__ import annotations

import re
from typing import Any

from app.rag.document import ParsedDocument
from app.versioning import hash_value


def reference_edges(
    document: ParsedDocument,
    info: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
    accepted: set[str],
    rejected: set[str],
    rules: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges = []
    unresolved = []
    for block in document["blocks"]:
        if info[block["block_id"]]["role"] in {"furniture", "heading", "code"}:
            continue
        for match in re.finditer(rules.explicit_reference_pattern, block["text"], re.I):
            target = match.group("target")
            found = [s for s in sections if s["title"].casefold().startswith(target.casefold())]
            if len(found) != 1:
                unresolved.append(
                    {
                        "from": block["block_id"],
                        "target": target,
                        "reason": "missing_or_ambiguous_target",
                    }
                )
                continue
            section = found[0]
            members = [
                b
                for b in info.values()
                if (
                    b["section_id"] == section["id"]
                    or b["section_path"][: len(info[section["source_block_id"]]["section_path"])]
                    == info[section["source_block_id"]]["section_path"]
                )
                and b["role"] not in {"furniture", "heading"}
            ]
            destination = members[0]["block_id"] if members else section["source_block_id"]
            if destination == block["block_id"]:
                continue
            key = hash_value(
                [document["source_hash"], block["block_id"], destination, "references_section"]
            )[:32]
            edges.append(
                {
                    "id": key,
                    "from": block["block_id"],
                    "to": destination,
                    "relation": "references_section",
                    "state": "rejected" if key in rejected else "accepted",
                    "evidence": ["explicit_source_reference", "unique_existing_section"],
                    "target_section_id": section["id"],
                    "target_text": target,
                }
            )
    return edges, unresolved
