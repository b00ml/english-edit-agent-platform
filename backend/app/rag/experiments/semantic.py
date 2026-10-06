"""Sentence similarity candidates inside prose blocks only; structural borders win."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from app.rag.chunking.models import Chunk, ChunkingConfig
from app.rag.chunking.structural import split_structural, validate_structural
from app.rag.document import ParsedDocument
from app.versioning import hash_value


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or any(not math.isfinite(v) for v in [*a, *b]):
        raise ValueError("Invalid semantic vectors")
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    if not norm or not math.isfinite(norm):
        raise ValueError("Zero semantic vector")
    return sum(x * y for x, y in zip(a, b)) / norm


def semantic_chunks(
    document: ParsedDocument,
    cfg: ChunkingConfig,
    embed: Callable[[list[str]], list[list[float]]],
    threshold: float = 0.65,
    max_sentences: int = 128,
) -> tuple[list[Chunk], dict[str, Any]]:
    if not -1 <= threshold <= 1 or not 1 <= max_sentences <= 1024:
        raise ValueError("Invalid semantic experiment limits")
    baseline, diagnostics = split_structural(document, cfg)
    blocks = {b["block_id"]: b for b in document["blocks"]}
    eligible = {s["block_id"] for chunk in baseline for s in chunk.source_segments}
    replacement: dict[str, list[Chunk]] = {}
    scores = []
    inputs: list[str] = []
    ranges = []
    # Only unambiguous single-paragraph prose leaves. Tables/code/headings/units are never moved.
    for bid in sorted(eligible, key=lambda key: blocks[key]["order"]):
        block = blocks[bid]
        if block["block_type"] != "paragraph" or any(
            c.table_id for c in baseline if bid in c.block_ids
        ):
            continue
        involved = [c for c in baseline if bid in c.block_ids]
        if any(len(c.block_ids) != 1 for c in involved):
            continue
        spans = []
        start = 0
        for m in re.finditer(r"[。！？]|[.!?](?:\s|$)", block["text"]):
            end = m.end()
            spans.append((start, end))
            start = end
        if start < len(block["text"]):
            spans.append((start, len(block["text"])))
        spans = [(a, b) for a, b in spans if block["text"][a:b].strip()]
        if len(spans) < 2 or any(
            len(block["text"][a:b]) + len(involved[0].context_header) + 2 > cfg.chunk_size
            for a, b in spans
        ):
            continue
        if len(inputs) + len(spans) > max_sentences:
            raise ValueError("Semantic sentence cap exceeded; no provider call")
        ranges.append((bid, spans, len(inputs), involved[0]))
        inputs.extend(block["text"][a:b] for a, b in spans)
    vectors = embed(inputs) if inputs else []
    if len(vectors) != len(inputs):
        raise ValueError("Incomplete semantic vectors")
    for bid, spans, offset, base in ranges:
        block = blocks[bid]
        groups = []
        current: list[tuple[int, int]] = []
        for i, (a, b) in enumerate(spans):
            similarity = cosine(vectors[offset + i - 1], vectors[offset + i]) if i else 1.0
            if i:
                scores.append({"block_id": bid, "sentence": i, "similarity": similarity})
            if current:
                trial = block["text"][current[0][0] : b]
                value = base.context_header + "\n\n" + trial
                if (
                    similarity < threshold
                    or len(value) > cfg.chunk_size
                    or cfg.token_limit
                    and len(value.encode()) > cfg.token_limit
                ):
                    groups.append(current)
                    current = []
            current.append((a, b))
        if current:
            groups.append(current)
        chunks = []
        for group in groups:
            a, b = group[0][0], group[-1][1]
            content = block["text"][a:b]
            sa = block["meta"]["content_start"] + a
            segment = {
                **base.source_segments[0],
                "source_start": sa,
                "source_end": sa + len(content),
                "content_start": 0,
                "content_end": len(content),
                "content_hash": hash_value(content),
            }
            chunks.append(
                Chunk(
                    content=content,
                    start=sa,
                    end=sa + len(content),
                    context_header=base.context_header,
                    section_path=list(base.section_path),
                    block_ids=[bid],
                    source_segments=[segment],
                    source_locator=base.source_locator,
                    page_no=block["page_no"],
                    logical_unit_id=base.logical_unit_id,
                    chunker_version="experiment-semantic-v1",
                )
            )
        replacement[bid] = chunks
    output = []
    done = set()
    for chunk in baseline:
        bid = chunk.block_ids[0]
        if bid in replacement:
            if bid not in done:
                output.extend(replacement[bid])
                done.add(bid)
        else:
            output.append(chunk)
    output.sort(key=lambda c: (c.start, c.end))
    validate_structural(output, document, eligible, cfg)
    return output, {
        "threshold": threshold,
        "sentence_inputs": len(inputs),
        "boundaries": scores,
        "prose_blocks_changed": sorted(replacement),
        "protected_layout": True,
        "baseline_signature": diagnostics.plan_signature,
        "scope": (
            "single-prose-block experiment; unchanged structured units "
            "are not evidence of semantic benefit"
        ),
    }
