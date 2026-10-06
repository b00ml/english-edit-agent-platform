"""Bounded alias expansion and optional structured LLM expansion with tracing."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.engine.trace import compute_cost, record_trace, token_breakdown, usage_is_reported
from app.errors import RagProviderError, TracePersistenceError
from app.prompt_loader import load_prompt, render
from app.rag.document import text_hash
from app.rag.knowledge_points import KnowledgeCatalog, KnowledgeScope, normalized


class ExpandedQueries(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    queries: list[str] = Field(max_length=8)

    @field_validator("queries")
    @classmethod
    def bounded(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("扩展查询为空或超过上限")
        return values


def _client() -> OpenAI:
    if not settings.RAG_QUERY_EXPANSION_MODEL:
        raise RagProviderError("未配置 query expansion model")
    return OpenAI(
        base_url=settings.RAG_QUERY_EXPANSION_API_BASE or settings.LLM_API_BASE,
        api_key=settings.RAG_QUERY_EXPANSION_API_KEY or settings.LLM_API_KEY,
        timeout=settings.RAG_QUERY_EXPANSION_TIMEOUT,
        max_retries=0,
    )


def llm_queries(query: str, scope: KnowledgeScope, trace: dict[str, Any]) -> list[str]:
    system = load_prompt("rag-expansion-system.st")
    user_template = load_prompt("rag-expansion-user.st")
    user = render(
        user_template,
        {
            "query": query,
            "knowledge_point": scope.canonical_name or "",
            "maximum": settings.RAG_QUERY_EXPANSION_MAX - 1,
        },
    )
    started = time.perf_counter()
    usage: Any = None  # Provider-specific usage object, processed by the shared trace layer.
    error: str | None = None
    expanded: list[str] = []
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
        parsed = ExpandedQueries.model_validate(
            json.loads(response.choices[0].message.content or "")
        )
        expanded = parsed.queries[: max(0, settings.RAG_QUERY_EXPANSION_MAX - 1)]
        return expanded
    except Exception as exc:  # noqa: BLE001 - record failure and re-raise, caller decides fallback
        error = type(exc).__name__
        raise
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
            trace_id=trace.get("trace_id") or f"rag-expansion:{uuid.uuid4()}",
            task_id=trace.get("task_id"),
            template_id=trace.get("template_id"),
            tenant_id=trace.get("tenant_id"),
            model=settings.RAG_QUERY_EXPANSION_MODEL or "unconfigured",
            stage="rag_expand",
            latency_ms=(time.perf_counter() - started) * 1000,
            prompt_version=text_hash(system + user_template),
            cost=compute_cost(usage, settings.RAG_QUERY_EXPANSION_MODEL),
            input_data={
                "query_hash": text_hash(query),
                "query": query,
                "knowledge_point": scope.canonical_name,
                "max_queries": settings.RAG_QUERY_EXPANSION_MAX,
            },
            output_data={"error_type": error, "queries": expanded},
            success=error is None,
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            prompt_cost=tokens["prompt_cost"],
            completion_cost=tokens["completion_cost"],
            usage_reported=usage_is_reported(usage, "rag_expand"),
        )


def expand_queries(
    query: str,
    scope: KnowledgeScope,
    catalog: KnowledgeCatalog,
    diagnostics: dict[str, Any],
    trace: dict[str, Any],
) -> list[str]:
    if settings.RAG_QUERY_EXPANSION_MODE == "off":
        diagnostics["expansion_status"] = "disabled"
        return [query.strip()]
    point = catalog.resolve(query)
    canonical = point.canonical_name if point else query.strip()
    proposed = [canonical]
    if point:
        proposed += [query.strip(), *point.aliases]
    if settings.RAG_QUERY_EXPANSION_MODE == "llm" and settings.RAG_QUERY_EXPANSION_MAX > 1:
        try:
            proposed = [canonical, *llm_queries(query, scope, trace), *proposed[1:]]
            diagnostics["expansion_status"] = "llm"
        except TracePersistenceError:
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - optional query enhancement falls back to original/aliases
            diagnostics.setdefault("fallbacks", []).append(
                {"stage": "query_expansion", "error_type": type(exc).__name__}
            )
            diagnostics["expansion_status"] = "degraded"
    else:
        diagnostics["expansion_status"] = "aliases"
    # Original free-text query is always retained (including meaningful casing).
    proposed = [query.strip(), *proposed]
    result, seen = [], set()
    for value in proposed:
        key = normalized(value)
        if key and key not in seen and len(value) <= 512:
            seen.add(key)
            result.append(value)
    return result[: settings.RAG_QUERY_EXPANSION_MAX]
