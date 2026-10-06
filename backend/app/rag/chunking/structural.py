"""STR-5 logical-unit leaf splitting with exact discontiguous source coordinates."""

from __future__ import annotations

import copy
from typing import Any

from app.config import settings
from app.errors import ChunkingError
from app.rag.chunking.models import Chunk, ChunkDiagnostics, ChunkingConfig
from app.rag.document import ParsedDocument
from app.rag.structure import build_structure
from app.versioning import hash_value

VERSION = "rag-chunker-structure-v1"


def validate_structural(
    chunks: list[Chunk], document: ParsedDocument, eligible: set[str], cfg: ChunkingConfig
) -> None:
    blocks = {b["block_id"]: b for b in document["blocks"]}
    covered = {bid: bytearray(len(blocks[bid]["text"])) for bid in eligible}
    last = -1
    for chunk in chunks:
        if (
            not chunk.source_segments
            or not chunk.content.strip()
            or len(chunk.embedding_content) > cfg.chunk_size
            or cfg.token_limit
            and len(chunk.embedding_content.encode()) > cfg.token_limit
        ):
            raise ChunkingError("structural chunk empty/over budget/no source")
        if chunk.start < last:
            raise ChunkingError("nonmonotonic structural source")
        last = chunk.start
        body_covered = bytearray(len(chunk.content))
        for segment in chunk.source_segments:
            bid = segment["block_id"]
            block = blocks.get(bid)
            if block is None or bid not in eligible:
                raise ChunkingError("unknown/ineligible structural member")
            a, b = segment["source_start"], segment["source_end"]
            lo, hi = segment["content_start"], segment["content_end"]
            source = document["text"][a:b]
            if (
                not (block["meta"]["content_start"] <= a < b <= block["meta"]["content_end"])
                or chunk.content[lo:hi] != source
                or segment["content_hash"] != hash_value(source)
            ):
                raise ChunkingError("structural provenance mismatch")
            if not 0 <= lo < hi <= len(chunk.content) or any(body_covered[lo:hi]):
                raise ChunkingError("structural content segment overlap/invalid range")
            body_covered[lo:hi] = b"\x01" * (hi - lo)
            covered[bid][
                a - block["meta"]["content_start"] : b - block["meta"]["content_start"]
            ] = b"\x01" * (b - a)
        if any(not flag and not char.isspace() for flag, char in zip(body_covered, chunk.content)):
            raise ChunkingError("unattributed structural content")
    if any(
        any(
            not flag and not char.isspace() for flag, char in zip(covered[bid], blocks[bid]["text"])
        )
        for bid in eligible
    ):
        raise ChunkingError("structural source coverage incomplete")


def split_structural(
    document: ParsedDocument, cfg: ChunkingConfig, structure: dict[str, Any] | None = None
) -> tuple[list[Chunk], ChunkDiagnostics]:
    from app.rag.chunking.strategy import _range_chunks, _table_chunks

    plan = structure or build_structure(document, max_blocks=settings.RAG_STRUCTURE_MAX_BLOCKS)
    info = {b["block_id"]: b for b in plan["blocks"]}
    originals = {b["block_id"]: b for b in document["blocks"]}
    parent = {
        bid: bid
        for bid, b in info.items()
        if b["role"] != "furniture" and originals[bid]["text"].strip()
    }

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for edge in plan["edges"]:
        if edge["state"] != "accepted" or edge["relation"] in {
            "table_continues",
            "table_cell_continues",
            "references_section",
        }:
            continue
        a, b = edge["from"], edge["to"]
        if a not in parent or b not in parent:
            continue
        # Protected table/code are separate leaves; relationships join at retrieval.
        if any(originals[key]["block_type"] in {"table", "table_row", "code"} for key in [a, b]):
            continue
        parent[root(b)] = root(a)
    groups: dict[str, list[str]] = {}
    for bid in parent:
        groups.setdefault(root(bid), []).append(bid)
    output = []
    eligible = set()
    excluded = []
    for members in groups.values():
        members.sort(key=lambda bid: originals[bid]["order"])
        if all(info[bid]["role"] == "heading" for bid in members):
            excluded.extend(members)
            continue
        eligible.update(members)
        blocks = []
        mappings = []
        parts = []
        cursor = 0
        for bid in members:
            block = copy.deepcopy(originals[bid])
            start = cursor
            parts.append(block["text"])
            cursor += len(block["text"])
            block["meta"]["content_start"], block["meta"]["content_end"] = start, cursor
            mappings.append((start, cursor, originals[bid], info[bid]))
            blocks.append(block)
            cursor += 2
        virtual = "\n\n".join(parts)
        body = next((i for i in members if info[i]["role"] != "heading"), members[0])
        path = list(info[body]["section_path"])
        context = " > ".join(path)
        unit = hash_value([VERSION, *members])[:32]
        if len(blocks) == 1 and blocks[0]["block_type"] == "table":
            chunks = _table_chunks(virtual, blocks[0], cfg, "heading", context, path)
        else:
            chunks = _range_chunks(virtual, 0, len(virtual), blocks, cfg, "heading", context, path)
        for chunk in chunks:
            segments = []
            for a, b, original, item in mappings:
                lo, hi = max(a, chunk.start), min(b, chunk.end)
                if lo >= hi:
                    continue
                sa = original["meta"]["content_start"] + lo - a
                sb = sa + hi - lo
                segments.append(
                    {
                        "block_id": original["block_id"],
                        "page_no": original["page_no"],
                        "source_start": sa,
                        "source_end": sb,
                        "content_start": lo - chunk.start,
                        "content_end": hi - chunk.start,
                        "content_hash": hash_value(document["text"][sa:sb]),
                        "source_locator": dict(original["source_locator"]),
                        "table_id": original["meta"].get("table_id"),
                        "source_role": item["role"],
                        "logical_section_id": item["section_id"],
                    }
                )
            if not segments:
                continue
            chunk.start, chunk.end = segments[0]["source_start"], segments[-1]["source_end"]
            chunk.block_ids = [s["block_id"] for s in segments]
            chunk.source_segments = segments
            chunk.source_locator = [
                {
                    **s["source_locator"],
                    "block_id": s["block_id"],
                    "block_start": s["source_start"]
                    - originals[s["block_id"]]["meta"]["content_start"],
                    "block_end": s["source_end"]
                    - originals[s["block_id"]]["meta"]["content_start"],
                }
                for s in segments
            ]
            pages = {s["page_no"] for s in segments}
            chunk.page_no = next(iter(pages)) if len(pages) == 1 else None
            chunk.logical_unit_id = unit
            chunk.chunker_version = VERSION
            output.append(chunk)
    output.sort(key=lambda chunk: (chunk.start, chunk.end))
    validate_structural(output, document, eligible, cfg)
    lengths = [len(c.content) for c in output]
    signature = hash_value(
        {
            "structure": plan["signature"],
            "layout": VERSION,
            "config": cfg.__dict__,
            "inputs": [hash_value(c.embedding_content) for c in output],
            "segments": [c.source_segments for c in output],
        }
    )
    return output, ChunkDiagnostics(
        strategy_requested=cfg.strategy,
        strategy_used="structure",
        chunk_count=len(output),
        min_length=min(lengths, default=0),
        max_length=max(lengths, default=0),
        coverage_ratio=1.0,
        hard_splits=sum(c.hard_split for c in output),
        table_chunks=sum(bool(c.table_id) for c in output),
        max_embedding_bytes=max((len(c.embedding_content.encode()) for c in output), default=0),
        warnings=list(dict.fromkeys(w for c in output for w in c.warning)),
        source_coverage_scope=(
            "eligible_body_and_attached_headings; layout furniture and heading-only "
            "navigation preserved in source not embedded"
        ),
        excluded_navigation_blocks=excluded
        + [bid for bid, b in info.items() if b["role"] == "furniture"],
        layout="structure",
        plan_signature=signature,
    )
