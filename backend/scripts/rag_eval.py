"""Explicit real retrieval evaluator. No corpus writes; gold loaded before retrieval.

Run in backend with --gold <JSON> --output <JSON>. Each query calls real embedding.
PageHit/MRR are final-context metrics, NOT pure ANN leaf recall or factual accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import KnowledgeChunk, KnowledgeDocument  # noqa: E402
from app.rag.retriever import retrieve  # noqa: E402
from app.rag.tables import cells  # noqa: E402
from app.versioning import hash_value  # noqa: E402


class Relevant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    pages: list[int] = Field(min_length=1)
    grade: int = Field(default=1, ge=1, le=3)
    required_terms: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list, max_length=128)
    table_id: str | None = None
    required_rows: list[list[str]] = Field(default_factory=list, max_length=64)
    required_logical_rows: list[list[str]] = Field(default_factory=list, max_length=64)

    @field_validator("required_rows", "required_logical_rows")
    @classmethod
    def valid_rows(cls, rows: list[list[str]]) -> list[list[str]]:
        if any(
            not row or len(row) > 32 or any(not term.strip() or len(term) > 128 for term in row)
            for row in rows
        ):
            raise ValueError("row tuples require 1-32 nonblank anchors of at most 128 characters")
        return rows


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=512)
    knowledge_point: str | None = None
    category: str = Field(default="regression", max_length=32)
    relevant: list[Relevant] = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)


def normalized(value: str) -> str:
    return "".join(value.split()).casefold()


def matches(citation: dict[str, Any], unit: Relevant, *, precise: bool) -> bool:
    page_matches = citation.get("page_no") in unit.pages
    if citation.get("diagnostic_candidate") is True:
        page_matches |= bool(set(unit.pages).intersection(citation.get("pages", [])))
    if citation.get("document_id") != unit.document_id or not page_matches:
        return False
    if not precise:
        return True
    matched_ids = {citation.get("chunk_id"), *citation.get("matched_child_ids", [])}
    return (
        (not unit.chunk_ids or bool(matched_ids.intersection(unit.chunk_ids)))
        and (
            not unit.source_block_ids
            or citation.get("block_id") in unit.source_block_ids
            or (
                citation.get("diagnostic_candidate") is True
                and bool(
                    set(unit.source_block_ids).intersection(citation.get("source_block_ids", []))
                )
            )
        )
        and (unit.table_id is None or citation.get("table_id") == unit.table_id)
    )


def ranking(
    case: Case, returned: list[dict[str, Any]], *, precise: bool
) -> tuple[bool, float, float, float]:
    grades: list[int] = []
    positions: list[int] = []
    credited: set[int] = set()
    for citation in returned:
        rows = {
            i for i, unit in enumerate(case.relevant) if matches(citation, unit, precise=precise)
        }
        grades.append(max((case.relevant[i].grade for i in rows - credited), default=0))
        positions.append(int(citation.get("bundle_rank", len(positions) + 1)))
        credited.update(rows)
    first = next((position for position, grade in zip(positions, grades) if grade), None)
    dcg = sum(
        (2**grade - 1) / math.log2(position + 1) for position, grade in zip(positions, grades)
    )
    ideal = sorted((unit.grade for unit in case.relevant), reverse=True)[: case.top_k]
    idcg = sum((2**grade - 1) / math.log2(i + 2) for i, grade in enumerate(ideal))
    return (
        bool(first),
        1 / first if first else 0,
        len(credited) / len(case.relevant),
        min(1, dcg / idcg) if idcg else 0,
    )


def score(
    case: Case, citations: list[dict[str, Any]], diagnostics: dict[str, Any] | None = None
) -> dict[str, Any]:
    selected = citations[: case.top_k]
    returned: list[dict[str, Any]] = []
    for bundle_rank, entry in enumerate(selected, 1):
        if entry.get("protocol_version") == "rag-context-v2":
            # Only actually delivered segments count. A bundle's claimed child IDs are not evidence.
            returned.extend(
                {**segment, "bundle_rank": bundle_rank}
                for segment in entry.get("source_segments", [])
            )
        else:
            returned.append(entry)
    hit, mrr, recall, ndcg = ranking(case, returned, precise=False)
    unit_hit, unit_mrr, unit_recall, unit_ndcg = ranking(case, returned, precise=True)
    terms_supported = True
    row_checks: list[bool] = []
    logical_checks: list[bool] = []
    actual_ids = {segment.get("segment_id") for segment in returned if segment.get("segment_id")}
    actual_segments = {
        segment["segment_id"]: segment for segment in returned if segment.get("segment_id")
    }
    views = [
        view
        for bundle in (diagnostics or {}).get("context_bundles", [])
        for view in bundle.get("logical_table_views", [])
    ]
    unit_support: list[tuple[bool, bool]] = []
    for unit in case.relevant:
        supporting = [c for c in returned if matches(c, unit, precise=True)]
        content = normalized(" ".join(c.get("content", "") for c in supporting))
        terms_ok = all(normalized(term) in content for term in unit.required_terms)
        terms_supported &= terms_ok
        # Every expected tuple must coexist on one source-table line; matching the
        # right page or scattering anchors across unrelated rows is not enough.
        lines = [
            normalized(line)
            for c in supporting
            for line in c.get("content", "").splitlines()
            if line.strip().startswith("|")
        ]
        expected_rows = [
            bool(terms) and any(all(normalized(term) in line for term in terms) for line in lines)
            for terms in unit.required_rows
        ]
        row_checks.extend(expected_rows)
        logical_lines = []
        for view in views:
            view_ids = set(view.get("source_segment_ids", []))
            if (
                not view_ids
                or not view_ids.issubset(actual_ids)
                or not view.get("context_delivered")
            ):
                continue
            if unit.table_id and unit.table_id not in view.get("physical_table_ids", []):
                continue
            relevant_segments = [
                segment for segment in supporting if segment.get("segment_id") in view_ids
            ]
            if not relevant_segments:
                continue
            for row in view.get("rows", []):
                parts = [part for cell in row.get("cells", []) for part in cell.get("parts", [])]
                fidelity = True
                for cell in row.get("cells", []):
                    originals = []
                    for part in cell.get("parts", []):
                        segment = actual_segments.get(part.get("segment_id"))
                        values = cells(segment["content"].strip()) if segment else []
                        column = part.get("column")
                        if not isinstance(column, int) or not 0 <= column < len(values):
                            fidelity = False
                            break
                        originals.append(values[column])
                    if normalized(" ".join(originals)) != normalized(str(cell.get("text", ""))):
                        fidelity = False
                if (
                    fidelity
                    and parts
                    and all(part.get("segment_id") in actual_ids for part in parts)
                ):
                    logical_lines.append(
                        normalized(" ".join(str(cell.get("text", "")) for cell in row["cells"]))
                    )
        checked_logical = [
            any(all(normalized(term) in line for term in terms) for line in logical_lines)
            for terms in unit.required_logical_rows
        ]
        logical_checks.extend(checked_logical)
        unit_support.append((terms_ok, all(expected_rows) and all(checked_logical)))
    result: dict[str, Any] = {
        "page_hit": hit,
        "page_mrr": mrr,
        "relevant_unit_recall": recall,  # v1 compatibility: page/document units only
        "page_ndcg": ndcg,
        "unit_hit": unit_hit,
        "unit_mrr": unit_mrr,
        "precise_unit_recall": unit_recall,
        "unit_ndcg": unit_ndcg,
        "terms_in_supporting_context": terms_supported,
        "same_row_support": all(row_checks) if row_checks else None,
        "row_tuple_recall": sum(row_checks) / len(row_checks) if row_checks else None,
        "row_tuple_count": len(row_checks),
        "supported_row_tuples": sum(row_checks),
        "logical_row_support": all(logical_checks) if logical_checks else None,
        "logical_row_count": len(logical_checks),
        "supported_logical_rows": sum(logical_checks),
        "context_complete": unit_recall == 1
        and terms_supported
        and all(row_checks)
        and all(logical_checks)
        and not any(
            c.get("truncated") or c.get("partial_row")
            for c in returned
            if any(matches(c, unit, precise=True) for unit in case.relevant)
        ),
        "snapshot_hash_valid": all(
            c.get("content_hash") == hash_value(c.get("content", ""))
            for c in [*selected, *returned]
        ),
        "actual_segment_count": len(returned),
        "returned_bundle_count": len(selected),
        "returned_documents": [c.get("document_id") for c in returned],
        "returned_pages": [c.get("page_no") for c in returned],
    }
    if diagnostics is not None and diagnostics.get("diagnostic_version") == "rag-retrieval-v2":
        lanes = diagnostics.get("lanes", {})
        groups = {
            "vector": [
                c
                for key, lane in lanes.items()
                if key.startswith("vector:")
                for c in lane["candidates"]
            ],
            "keyword": [
                c
                for key, lane in lanes.items()
                if key.startswith("keyword:")
                for c in lane["candidates"]
            ],
            "fusion": diagnostics.get("fusion", []),
            "rerank": diagnostics.get("rerank", []),
        }
        stage_hits = {
            name: any(matches(c, unit, precise=True) for c in rows for unit in case.relevant)
            for name, rows in groups.items()
        }
        result["stage_hits"] = stage_hits  # any relevant unit, retained for v2 compatibility
        result["stage_unit_recall"] = {
            name: sum(any(matches(c, unit, precise=True) for c in rows) for unit in case.relevant)
            / len(case.relevant)
            for name, rows in groups.items()
        }
        failures = []
        for index, unit in enumerate(case.relevant):
            if any(matches(c, unit, precise=True) for c in returned):
                terms_ok, rows_ok = unit_support[index]
                if not rows_ok:
                    failures.append({"unit": index, "stage": "source_row_support"})
                elif not terms_ok:
                    failures.append({"unit": index, "stage": "content_support"})
                continue
            available = {
                name: any(matches(c, unit, precise=True) for c in rows)
                for name, rows in groups.items()
            }
            stage = (
                "recall"
                if not (available["vector"] or available["keyword"])
                else (
                    "fusion"
                    if not available["fusion"]
                    else "rerank" if not available["rerank"] else "context_or_top_k"
                )
            )
            failures.append({"unit": index, "stage": stage})
        result["failure_details"] = failures
        stages = {failure["stage"] for failure in failures}
        result["failure_stage"] = (
            None if not stages else next(iter(stages)) if len(stages) == 1 else "multiple"
        )
    return result


def corpus_manifest(session: Session, tenant_id: str | None) -> dict[str, Any]:
    # Match retrieve's tenant semantics, including legacy chunks.
    # Never include provider keys or body text in the report.
    documents = (
        session.query(KnowledgeDocument)
        .filter(KnowledgeDocument.tenant_id == tenant_id)
        .order_by(KnowledgeDocument.id)
        .all()
    )
    chunks = (
        session.query(KnowledgeChunk)
        .filter(KnowledgeChunk.tenant_id == tenant_id)
        .order_by(KnowledgeChunk.id)
        .all()
    )
    snapshot = {
        "documents": [
            {
                "id": row.id,
                "revision": (row.meta or {}).get("index_revision"),
                "source_hash": row.source_hash,
                "content_hash": row.content_hash,
            }
            for row in documents
        ],
        "chunks": [
            {
                "id": row.id,
                "document_id": row.document_id,
                "content_hash": hash_value(row.content),
                "embedding_content_hash": row.embedding_content_hash,
                "model": row.embedding_model,
                "dimension": row.embedding_dimension,
                "embedding_hash": (
                    hash_value([float(value) for value in row.embedding])
                    if row.embedding is not None
                    else None
                ),
                "metadata_hash": hash_value(row.meta or {}),
                "search_hash": hash_value(row.search_text),
                "parent_id": row.parent_chunk_id,
                "previous_id": row.prev_chunk_id,
                "next_id": row.next_chunk_id,
            }
            for row in chunks
        ],
    }
    return {"sha256": hash_value(snapshot), "documents": len(documents), "chunks": len(chunks)}


def runtime_settings() -> dict[str, Any]:
    return {
        key: getattr(settings, key)
        for key in (
            "EMBEDDING_MODEL_NAME",
            "EMBEDDING_DIM",
            "RAG_RETRIEVAL_METHOD",
            "RAG_CJK_KEYWORD_MODE",
            "RAG_KEYWORD_MAX_TERMS",
            "RAG_CANDIDATE_POOL",
            "RAG_MIN_SIMILARITY",
            "RAG_RRF_K",
            "RAG_RERANK_MODE",
            "RAG_QUERY_EXPANSION_MODE",
            "RAG_QUERY_EXPANSION_MAX",
            "RAG_CONTEXT_MAX_CHARS",
            "RAG_CONTEXT_TOKEN_LIMIT",
            "RAG_NEIGHBOR_WINDOW",
            "RAG_CONTEXT_MODE",
            "RAG_CHUNK_LAYOUT",
            "RAG_QUERY_PLANNING_MODE",
            "RAG_QUERY_PLAN_MAX_PARTS",
            "RAG_QUERY_PLAN_MAX_QUERIES",
            "RAG_STRUCTURE_ENABLED",
            "RAG_STRUCTURE_MAX_BLOCKS",
            "RAG_STRUCTURE_MAX_LEAFS",
            "RAG_CONTEXT_BUNDLE_MAX_MEMBERS",
            "RAG_CONTEXT_BUNDLE_MAX_HOPS",
            "RAG_CONTEXT_MAX_SEGMENTS",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--top-k", type=int, choices=range(1, 11), default=None)
    parser.add_argument("--run-id", default=uuid.uuid4().hex)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", args.run_id):
        parser.error("run-id must contain 1-32 lowercase letters/digits/hyphens/underscores")
    raw = args.gold.read_bytes()
    cases = [Case.model_validate(item) for item in json.loads(raw)]
    if not cases or len(cases) > 1000 or len({case.id for case in cases}) != len(cases):
        parser.error("gold requires 1-1000 cases with unique IDs")
    results: list[dict[str, Any]] = []
    with SessionLocal() as session:
        corpus_before = corpus_manifest(session, args.tenant_id)
        for case in cases:
            # Keep original gold immutable; score only the explicitly selected budget.
            effective_case = case.model_copy(update={"top_k": args.top_k}) if args.top_k else case
            citations: list[dict[str, Any]] = []
            diagnostics: dict[str, Any] = {}
            retrieve(
                session,
                case.query,
                knowledge_point=case.knowledge_point,
                top_k=effective_case.top_k,
                tenant_id=args.tenant_id,
                trace_id=f"rag-eval:{args.run_id}:{case.id}",
                details=citations,
                diagnostics=diagnostics,
            )
            results.append(
                {
                    "case": case.model_dump(),
                    "effective_top_k": effective_case.top_k,
                    "metrics": score(effective_case, citations, diagnostics),
                    "citations": citations,
                    "diagnostics": diagnostics,
                }
            )
        # End the read transaction so a concurrent commit is visible for the final fingerprint.
        session.rollback()
        corpus_after = corpus_manifest(session, args.tenant_id)
    report = {
        "evaluator_version": "rag-eval-v7",
        "top_k_override": args.top_k,
        "run_id": args.run_id,
        "runtime": runtime_settings(),
        "corpus_before": corpus_before,
        "corpus_after": corpus_after,
        "corpus_unchanged": corpus_before == corpus_after,
        "gold_sha256": hashlib.sha256(raw).hexdigest(),
        "cases": len(cases),
        "scope": (
            "final-context page/document relevance; "
            "precise units and source-row anchor checks when annotated; "
            "not leaf ANN Recall@K or answer factual correctness"
        ),
        "metrics": {
            key: sum(float(row["metrics"][key]) for row in results) / len(results)
            for key in [
                "page_hit",
                "page_mrr",
                "relevant_unit_recall",
                "page_ndcg",
                "terms_in_supporting_context",
                "unit_hit",
                "unit_mrr",
                "precise_unit_recall",
                "unit_ndcg",
                "context_complete",
            ]
        },
        "row_metrics": {
            "tuples": sum(row["metrics"]["row_tuple_count"] for row in results),
            "supported_tuples": sum(row["metrics"]["supported_row_tuples"] for row in results),
            "cases": sum(row["metrics"]["same_row_support"] is not None for row in results),
            "supported_cases": sum(row["metrics"]["same_row_support"] is True for row in results),
            "tuple_recall": (
                sum(
                    row["metrics"]["row_tuple_recall"]
                    for row in results
                    if row["metrics"]["row_tuple_recall"] is not None
                )
                / sum(row["metrics"]["row_tuple_recall"] is not None for row in results)
                if any(row["metrics"]["row_tuple_recall"] is not None for row in results)
                else None
            ),
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "metrics": report["metrics"]}))
    return 0 if report["corpus_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
