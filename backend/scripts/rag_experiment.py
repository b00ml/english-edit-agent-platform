"""Explicit STR-6 experiments; reports only, never writes knowledge/index state.

Dry-run is default. --run --paid-embedding authorizes real final-vector queries.
Late runs require explicit local token-capable safetensors weights; final-vector API
cannot provide contextual token states. No implicit model installation/downloads.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import KnowledgeDocument  # noqa: E402
from app.rag.chunking.structural import split_structural  # noqa: E402
from app.rag.embedding import embed_texts  # noqa: E402
from app.rag.experiments.contextual import prefixes  # noqa: E402
from app.rag.experiments.late import LocalTokenEncoder, pool_tokens  # noqa: E402
from app.rag.experiments.semantic import cosine, semantic_chunks  # noqa: E402
from app.rag.indexer import configured_chunking  # noqa: E402
from app.rag.structure.runtime import document_snapshot  # noqa: E402
from app.versioning import hash_value  # noqa: E402


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=512)
    expected_block_ids: list[str] = Field(default_factory=list, max_length=64)
    required_terms: list[str] = Field(default_factory=list, max_length=64)
    top_k: int = Field(default=3, ge=1, le=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input", type=Path, help="canonical ParsedDocument JSON; not a native engine dump"
    )
    source.add_argument("--document-id")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument(
        "--variant", choices=["structure", "semantic", "contextual", "late"], default="structure"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--paid-embedding", action="store_true")
    parser.add_argument("--allow-llm-context", action="store_true")
    parser.add_argument("--late-model-dir", type=Path)
    parser.add_argument("--body-size", type=int, default=384)
    args = parser.parse_args()
    if args.run and args.variant != "late" and not args.paid_embedding:
        parser.error("Real embedding requires --paid-embedding")
    if args.allow_llm_context and (not args.run or args.variant != "contextual"):
        parser.error("LLM context is explicit contextual-run only")
    if args.variant == "late" and args.run and not args.late_model_dir:
        parser.error(
            "Late needs explicit local token-capable model; final-vector cloud API unsupported"
        )
    if not 64 <= args.body_size <= settings.RAG_CHUNK_SIZE:
        parser.error("body-size must fit production character cap")
    if args.input:
        if args.input.stat().st_size > settings.RAG_EXPERIMENT_MAX_INPUT_BYTES:
            parser.error("Input exceeds experiment limit")
        document = json.loads(args.input.read_text(encoding="utf-8-sig"))
    else:
        with SessionLocal() as db:
            row = (
                db.query(KnowledgeDocument)
                .filter_by(id=args.document_id, tenant_id=args.tenant_id)
                .first()
            )
            if row is None:
                parser.error("Document absent in explicit tenant scope")
            document = document_snapshot(row)
            if (
                len(json.dumps(document, ensure_ascii=False).encode())
                > settings.RAG_EXPERIMENT_MAX_INPUT_BYTES
            ):
                parser.error("Document exceeds experiment limit")
    before = hash_value(document)
    cfg = replace(
        configured_chunking("structure"),
        chunk_size=args.body_size,
        chunk_overlap=min(settings.RAG_CHUNK_OVERLAP, args.body_size // 2),
    ).normalized()
    chunks, diagnostics = split_structural(document, cfg)
    if len(chunks) > settings.RAG_EXPERIMENT_MAX_CHUNKS:
        parser.error("Chunk cap exceeded before model call")
    cases = []
    gold_hash = None
    if args.gold:
        raw = args.gold.read_bytes()
        if len(raw) > settings.RAG_EXPERIMENT_MAX_INPUT_BYTES:
            parser.error("Gold exceeds limit")
        cases = [Query.model_validate(value) for value in json.loads(raw)]
        if not cases or len(cases) > 128 or len({q.id for q in cases}) != len(cases):
            parser.error("Gold requires 1-128 unique queries")
        gold_hash = hash_value(raw.decode("utf-8"))
    info: dict[str, Any] = {}
    trace_id = "rag-exp:" + hash_value([before, args.variant, time.time_ns()])[:32]
    calls = []

    def embed(values: list[str]) -> list[list[float]]:
        result = []
        for i in range(0, len(values), settings.EMBEDDING_PROVIDER_BATCH_LIMIT):
            inputs = values[i : i + settings.EMBEDDING_PROVIDER_BATCH_LIMIT]
            calls.append({"texts": len(inputs), "input_hash": hash_value(inputs)})
            vectors = embed_texts(inputs, tenant_id=args.tenant_id, trace_id=trace_id)
            if len(vectors) != len(inputs):
                raise ValueError("Incomplete experiment embedding response")
            result.extend(vectors)
        return result

    vectors = []
    query_vectors = []
    inputs = [c.embedding_content for c in chunks]
    started = time.perf_counter()
    if args.run:
        if args.variant == "semantic":
            chunks, info = semantic_chunks(
                document,
                cfg,
                embed,
                settings.RAG_EXPERIMENT_SEMANTIC_THRESHOLD,
                settings.RAG_EXPERIMENT_MAX_SENTENCES,
            )
            inputs = [c.embedding_content for c in chunks]
        if args.variant == "contextual":
            inputs, metadata = prefixes(
                chunks,
                document,
                configured_chunking("structure"),
                llm=args.allow_llm_context,
                trace_id=trace_id,
                max_calls=settings.RAG_EXPERIMENT_MAX_LLM_CALLS,
            )
            info["derived_context"] = metadata
        if args.variant == "late":
            encoder = LocalTokenEncoder(
                args.late_model_dir,
                settings.RAG_EXPERIMENT_MAX_TOKENS,
                trace_id=trace_id,
                tenant_id=args.tenant_id,
            )
            artifact = encoder.encode(document["text"])
            vectors = pool_tokens(
                document["text"],
                artifact,
                [[(s["source_start"], s["source_end"]) for s in c.source_segments] for c in chunks],
                settings.RAG_EXPERIMENT_MAX_TOKENS,
            )
            for case in cases:
                value = encoder.encode(case.query)
                query_vectors.extend(
                    pool_tokens(
                        case.query,
                        value,
                        [[(0, len(case.query))]],
                        settings.RAG_EXPERIMENT_MAX_TOKENS,
                    )
                )
            info["local_model"] = encoder.model_identity
            info["transformer_before_pooling"] = True
            info["token_count"] = len(artifact["offsets"])
        else:
            if len(chunks) > settings.RAG_EXPERIMENT_MAX_CHUNKS:
                raise ValueError("Semantic result exceeds chunk cap")
            vectors = embed(inputs)
            query_vectors = embed([q.query for q in cases]) if cases else []
    results = []
    if args.run:
        for q, vector in zip(cases, query_vectors):
            ranked = sorted(range(len(chunks)), key=lambda i: -cosine(vector, vectors[i]))[
                : q.top_k
            ]
            relevant = [chunks[i] for i in ranked]
            actual_blocks = {s["block_id"] for c in relevant for s in c.source_segments}
            content = " ".join(c.content for c in relevant)
            results.append(
                {
                    "id": q.id,
                    "returned_chunk_indexes": ranked,
                    "block_ids": sorted(actual_blocks),
                    "source_complete": set(q.expected_block_ids) <= actual_blocks
                    and all(term.casefold() in content.casefold() for term in q.required_terms),
                    "scope": "source block/keyword support, not answer correctness",
                }
            )
    assert hash_value(document) == before, "Experiment must not mutate source"
    report = {
        "version": "rag-experiment-v1",
        "variant": args.variant,
        "status": "executed" if args.run else "dry_run_prepared",
        "trace_id": trace_id,
        "config": asdict(cfg),
        "source_hash_before": before,
        "source_hash_after": hash_value(document),
        "gold_hash": gold_hash,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "model": (
            settings.EMBEDDING_MODEL_NAME if args.variant != "late" else info.get("local_model")
        ),
        "dimension": len(vectors[0]) if vectors else None,
        "production_index_changed": False,
        "provider_batches": calls,
        "llm_context_enabled": args.allow_llm_context,
        "chunks": [
            {
                "content": c.content,
                "embedding_input": inputs[i] if i < len(inputs) else c.embedding_content,
                "meta": c.as_meta(),
            }
            for i, c in enumerate(chunks)
        ],
        "diagnostics": diagnostics.as_dict(),
        "diagnostics_scope": (
            "structural baseline diagnostics; variant boundary details are separate"
        ),
        "source_coverage_verified": True,
        "derived_context_currencies_or_bills_verified": False,
        "variant_details": info,
        "results": results,
        "limitations": [
            "explicit experiment, not production optimization proof",
            "cloud final-vector API cannot supply token-level late pooling",
            "no quality certification when no gold/real model execution",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "variant": args.variant,
                "status": report["status"],
                "chunks": len(chunks),
                "provider_batches": len(calls),
                "gold_cases": len(results),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
