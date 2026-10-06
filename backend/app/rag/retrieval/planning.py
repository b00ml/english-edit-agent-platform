"""STR-4 complementary questions, explicit original retention, no scope rewriting."""

from __future__ import annotations

import json
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.engine.trace import compute_cost, record_trace, token_breakdown, usage_is_reported
from app.errors import TracePersistenceError
from app.prompt_loader import load_prompt, render
from app.rag.document import text_hash
from app.rag.knowledge_points import load_catalog, normalized
from app.rag.retrieval.expansion import _client
from app.rag.retrieval.models import Candidate


class Parts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parts: list[str] = Field(max_length=4)

    @field_validator("parts")
    @classmethod
    def bounded(cls, values: list[str]) -> list[str]:
        if any(not x.strip() or len(x) > 256 for x in values):
            raise ValueError("Invalid complementary question")
        return values


@lru_cache(maxsize=8)
def _policy(text: str) -> dict[str, Any]:
    result: dict[str, Any] = dict(yaml.safe_load(text))
    re.compile(result["separators"])
    return result


def question_parts(query: str, diagnostics: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    if settings.RAG_QUERY_PLANNING_MODE == "off":
        diagnostics["question_plan"] = {"mode": "off", "parts": []}
        return []
    policy_text = Path(__file__).with_name("planning.yml").read_text(encoding="utf-8")
    rules = _policy(policy_text)
    raw = [
        x.strip(" ,，。；;？?")
        for x in re.split(rules["separators"], query)
        if x.strip(" ,，。；;？?")
    ]
    parts = raw if len(raw) > 1 else []
    concepts: list[dict[str, Any]] = []
    hits = []
    for point in load_catalog().points.values():
        if point.status != "enabled":
            continue
        best = None
        for label in [point.canonical_name, *point.aliases]:
            if len(label) < 2:
                continue
            pattern = re.escape(label)
            if re.fullmatch(r"[A-Za-z -]+", label):
                pattern = r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])"
            match = re.search(pattern, query, re.I)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            hits.append((best, point.id, point.canonical_name))
    hits.sort()
    for position, key, name in hits:
        if key not in [c["id"] for c in concepts]:
            concepts.append({"id": key, "name": name, "position": position})
    if not parts and len(concepts) > 1:
        positions = sorted(set(c["position"] for c in concepts))
        if len(positions) > 1:
            parts = [
                query[
                    positions[i] : positions[i + 1] if i + 1 < len(positions) else len(query)
                ].strip(" ,，。；;？?与和、")
                for i in range(len(positions))
            ]
    if settings.RAG_QUERY_PLANNING_MODE == "llm":
        system = load_prompt("rag-plan-system.st")
        ut = load_prompt("rag-plan-user.st")
        user = render(ut, {"query": query, "maximum": settings.RAG_QUERY_PLAN_MAX_PARTS})
        started = time.perf_counter()
        usage = None
        error = None
        raw_response = None
        try:
            response = _client().chat.completions.create(
                model=settings.RAG_QUERY_EXPANSION_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0,
                max_tokens=settings.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS,
                response_format={"type": "json_object"},
            )
            raw_response = response.choices[0].message.content
            usage = response.usage
            parts = Parts.model_validate(
                json.loads(response.choices[0].message.content or "")
            ).parts
        except TracePersistenceError:
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - optional, observable degradation to deterministic plan
            error = type(exc).__name__
            diagnostics.setdefault("fallbacks", []).append(
                {"stage": "question_plan", "error_type": error}
            )
        finally:
            tokens = token_breakdown(usage, settings.RAG_QUERY_EXPANSION_MODEL)
            record_trace(
                snapshot_data={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_text": raw_response,
                    "model": settings.RAG_QUERY_EXPANSION_MODEL,
                    "temperature": 0,
                    "max_tokens": settings.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS,
                    "response_format": {"type": "json_object"},
                },
                trace_id=trace.get("trace_id") or "rag-plan:" + uuid.uuid4().hex,
                task_id=trace.get("task_id"),
                template_id=trace.get("template_id"),
                tenant_id=trace.get("tenant_id"),
                model=settings.RAG_QUERY_EXPANSION_MODEL or "unconfigured",
                stage="rag_plan",
                latency_ms=(time.perf_counter() - started) * 1000,
                prompt_version=text_hash(system + ut),
                cost=compute_cost(usage, settings.RAG_QUERY_EXPANSION_MODEL),
                input_data={"query": query},
                output_data={"error_type": error, "parts": parts},
                success=error is None,
                prompt_tokens=tokens["prompt_tokens"],
                completion_tokens=tokens["completion_tokens"],
                prompt_cost=tokens["prompt_cost"],
                completion_cost=tokens["completion_cost"],
                usage_reported=usage_is_reported(usage, "rag_plan"),
            )
    result = []
    seen = {normalized(query)}
    for part in parts:
        if len(part) < 2 or len(part) > 256 or normalized(part) in seen:
            continue
        seen.add(normalized(part))
        result.append(part)
    result = result[: settings.RAG_QUERY_PLAN_MAX_PARTS]
    diagnostics["question_plan"] = {
        "mode": settings.RAG_QUERY_PLANNING_MODE,
        "parts": result,
        "policy_hash": text_hash(policy_text),
        "scope_unchanged": True,
        "concept_navigation": concepts,
    }
    return result


def coverage_order(
    candidates: list[Candidate], part_indexes: list[int], diagnostics: dict[str, Any]
) -> list[Candidate]:
    if not part_indexes:
        return candidates
    output = []
    seen = set()
    assignments = []
    for index in part_indexes:
        ranked = sorted(
            candidates,
            key=lambda c: (
                -sum(
                    1 / (settings.RAG_RRF_K + rank)
                    for lane, rank in c.ranks.items()
                    if lane in {f"vector:{index}", f"keyword:{index}"}
                ),
                -c.rrf_score,
                c.row.id,
            ),
        )
        chosen = next(
            (
                c
                for c in ranked
                if c.row.id not in seen
                and any(lane in c.ranks for lane in {f"vector:{index}", f"keyword:{index}"})
            ),
            None,
        )
        if chosen:
            output.append(chosen)
            seen.add(chosen.row.id)
            assignments.append({"part_query_index": index, "seed_chunk_id": chosen.row.id})
    output.extend(c for c in candidates if c.row.id not in seen)
    diagnostics["coverage_selection"] = {
        "assignments": assignments,
        "note": "candidate allocation only, not answer verification",
    }
    return output
