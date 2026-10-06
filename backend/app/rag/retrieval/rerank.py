"""Optional/required rerank adapter for Cohere/Jina-style JSON endpoints."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.engine.trace import record_trace
from app.errors import RagProviderError, RagRerankRequiredError, TracePersistenceError
from app.rag.document import text_hash
from app.rag.retrieval.models import Candidate


class RankResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    index: int = Field(ge=0)
    relevance_score: float = Field(allow_inf_nan=False)


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    results: list[RankResult]


def _request(query: str, documents: list[str]) -> Any:
    if not settings.RAG_RERANK_URL or not settings.RAG_RERANK_MODEL:
        raise RagProviderError("rerank URL/model 未配置")
    headers = (
        {"Authorization": f"Bearer {settings.RAG_RERANK_API_KEY}"}
        if settings.RAG_RERANK_API_KEY
        else {}
    )
    with httpx.Client(timeout=settings.RAG_RERANK_TIMEOUT) as client:
        response = client.post(
            settings.RAG_RERANK_URL,
            headers=headers,
            json={
                "model": settings.RAG_RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
        )
        response.raise_for_status()
        return response.json()


def provider_scores(
    query: str, candidates: list[Candidate], trace: dict[str, Any]
) -> list[RankResult]:
    if not settings.RAG_RERANK_URL or not settings.RAG_RERANK_MODEL:
        raise RagProviderError("rerank URL/model 未配置")
    started = time.perf_counter()
    error: str | None = None
    try:
        documents = [f"{c.row.context_header or ''}\n\n{c.row.content}" for c in candidates]
        parsed = RerankResponse.model_validate(_request(query, documents))
        indices = [r.index for r in parsed.results]
        if sorted(indices) != list(range(len(candidates))):
            raise RagProviderError("rerank 索引缺失/重复/越界")
        return parsed.results
    except (
        Exception
    ) as exc:  # noqa: BLE001 - record invalid/costed provider attempt, never pretend success
        error = type(exc).__name__
        raise
    finally:
        # A reranker billing unit is provider-specific. Do not invent OpenAI token usage/cost.
        record_trace(
            trace_id=trace.get("trace_id") or f"rag-rerank:{uuid.uuid4()}",
            task_id=trace.get("task_id"),
            template_id=trace.get("template_id"),
            tenant_id=trace.get("tenant_id"),
            stage="rag_rerank",
            model=settings.RAG_RERANK_MODEL or "unconfigured",
            latency_ms=(time.perf_counter() - started) * 1000,
            cost=0.0,
            usage_reported=False,
            input_data={"query_hash": text_hash(query), "candidate_count": len(candidates)},
            output_data={"error_type": error, "billing_status": "unknown"},
            success=error is None,
        )


def rerank(
    query: str, candidates: list[Candidate], diagnostics: dict[str, Any], trace: dict[str, Any]
) -> list[Candidate]:
    diagnostics["rerank_mode"] = settings.RAG_RERANK_MODE
    if settings.RAG_RERANK_MODE == "off" or not candidates:
        diagnostics["rerank_status"] = (
            "disabled" if settings.RAG_RERANK_MODE == "off" else "no_candidates"
        )
        return candidates
    try:
        results = provider_scores(query, candidates, trace)
    except TracePersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - explicit optional/required policy boundary
        diagnostics["rerank_status"] = "degraded"
        diagnostics.setdefault("fallbacks", []).append(
            {"stage": "rerank", "error_type": type(exc).__name__}
        )
        if settings.RAG_RERANK_MODE == "required":
            raise RagRerankRequiredError("必需 rerank 不可用或返回无效") from exc
        return candidates
    for result in results:
        candidates[result.index].rerank_score = result.relevance_score
    diagnostics["rerank_status"] = "applied"
    return sorted(
        [
            c
            for c in candidates
            if c.rerank_score is not None and c.rerank_score >= settings.RAG_RERANK_THRESHOLD
        ],
        key=lambda c: (-float(c.rerank_score or 0), c.row.id),
    )
