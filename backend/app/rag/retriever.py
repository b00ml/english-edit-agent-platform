"""Hybrid retrieval orchestration, preserving the public P0 entry points."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, List, cast

from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Session

from app.config import settings
from app.engine.trace import record_lifecycle_event
from app.errors import (
    InvalidKnowledgeError,
    RagContextRequiredError,
    RagRerankRequiredError,
    TracePersistenceError,
)
from app.models import KnowledgeChunk
from app.rag.embedding import embed_texts
from app.rag.knowledge_points import ScopeMode, load_catalog
from app.rag.provenance import delivered_citations
from app.rag.retrieval import keyword, vector
from app.rag.retrieval.context import expand, format_source
from app.rag.retrieval.expansion import expand_queries
from app.rag.retrieval.fusion import fuse
from app.rag.retrieval.models import Candidate, candidate_summary, dialect_name
from app.rag.retrieval.planning import coverage_order, question_parts
from app.rag.retrieval.relation import expand_relations
from app.rag.retrieval.rerank import rerank

logger = logging.getLogger("app.rag.retriever")


def retrieve(
    session: Session,
    query: str,
    knowledge_point: str | None = None,
    top_k: int | None = None,
    tenant_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
    template_id: str | None = None,
    details: list[dict[str, Any]] | None = None,
    allow_all_tenants: bool = False,
    scope_mode: ScopeMode | None = None,
    document_ids: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
    source_name: str | None = None,
    section_path: list[str] | None = None,
    context_mode: str | None = None,
) -> List[str]:
    selected_context_mode = context_mode or settings.RAG_CONTEXT_MODE
    if selected_context_mode not in {"legacy", "relation"}:
        raise InvalidKnowledgeError("context_mode must be legacy/relation")
    if not query.strip():
        return []
    top_k = settings.RAG_TOP_K if top_k is None else top_k
    if (
        len(query) > 512
        or not 1 <= top_k <= 10
        or (
            document_ids is not None
            and (len(document_ids) > 32 or any(len(i) > 64 for i in document_ids))
        )
    ):
        raise InvalidKnowledgeError("检索 query/top_k/document scope 超过限制")
    catalog = load_catalog()
    scope = catalog.scope(knowledge_point, scope_mode or settings.RAG_KNOWLEDGE_SCOPE)
    if (
        source_name
        and len(source_name) > 128
        or section_path
        and (len(section_path) > 8 or any(len(s) > 128 for s in section_path))
    ):
        raise InvalidKnowledgeError("资料名称或章节过滤超限")
    scope.source_name = source_name
    scope.section_path = section_path or []
    effective_trace_id = trace_id or f"rag:{uuid.uuid4()}"
    info = diagnostics if diagnostics is not None else {}
    info.update(
        scope=scope.model_dump(),
        candidate_pool=max(top_k, settings.RAG_CANDIDATE_POOL),
        requested_top_k=top_k,
        context_max_chars=settings.RAG_CONTEXT_MAX_CHARS,
        context_token_limit=settings.RAG_CONTEXT_TOKEN_LIMIT,
        method=settings.RAG_RETRIEVAL_METHOD,
        diagnostic_version="rag-retrieval-v2",
        cjk_keyword_mode=settings.RAG_CJK_KEYWORD_MODE,
        keyword_max_terms=settings.RAG_KEYWORD_MAX_TERMS,
        fallbacks=[],
        lanes={},
        vector_backend=(
            "sqlite_reference" if dialect_name(session) == "sqlite" else "postgres_pgvector"
        ),
        keyword_backend=(
            "sqlite_literal_reference"
            if dialect_name(session) == "sqlite"
            else "postgres_fts_literal"
        ),
        context_budget=settings.RAG_CONTEXT_MAX_CHARS,
    )
    trace = {
        "trace_id": effective_trace_id,
        "task_id": task_id,
        "template_id": template_id,
        "tenant_id": tenant_id,
    }
    info["trace_id"] = effective_trace_id
    if document_ids == []:
        info.update(queries=[], final_count=0, scope_status="empty_document_scope")
        return []
    queries = expand_queries(query, scope, catalog, info, trace)
    complementary = question_parts(query, info, trace)
    original_queries = list(queries)
    queries = [query.strip(), *complementary, *original_queries[1:]]
    queries = list(dict.fromkeys(queries))[: settings.RAG_QUERY_PLAN_MAX_QUERIES]
    part_indexes = [queries.index(part) for part in complementary if part in queries]
    info["queries"] = queries
    lanes: dict[str, list[Candidate]] = {}
    pool = int(info["candidate_pool"])
    started = time.perf_counter()
    vectors = []
    try:
        if settings.EMBEDDING_DIM != cast(Vector, KnowledgeChunk.__table__.c.embedding.type).dim:
            raise InvalidKnowledgeError("查询 embedding 维度与 Schema 不一致")
        vectors = embed_texts(queries, **trace)
        if len(vectors) != len(queries):
            raise InvalidKnowledgeError("查询 embedding 返回数量不完整")
    except TracePersistenceError:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 - allow independent keyword recall without poisoning transaction
        vectors = []
        info["fallbacks"].append({"stage": "embedding", "error_type": type(exc).__name__})
    for index, value in enumerate(queries):
        routes: list[tuple[str, Callable[[], list[Candidate]]]] = (
            [
                (
                    "vector",
                    lambda: vector.recall(
                        session,
                        vectors[index],
                        scope,
                        tenant_id,
                        allow_all_tenants,
                        pool,
                        document_ids,
                    ),
                )
            ]
            if vectors
            else []
        )
        if settings.RAG_RETRIEVAL_METHOD == "hybrid":
            routes.append(
                (
                    "keyword",
                    lambda: keyword.recall(
                        session, value, scope, tenant_id, allow_all_tenants, pool, document_ids
                    ),
                )
            )
        for route, callback in routes:
            key = f"{route}:{index}"
            try:
                # Savepoint isolates a failed SQL route from a healthy independent route.
                transaction = (
                    session.begin_nested() if hasattr(session, "begin_nested") else nullcontext()
                )
                with transaction:
                    candidates = callback()
                if route == "keyword":
                    candidates = [
                        c
                        for c in candidates
                        if (c.keyword_score or 0) >= settings.RAG_KEYWORD_MIN_SCORE
                    ]
                lanes[key] = candidates
                info["lanes"][key] = {
                    "count": len(candidates),
                    "chunk_ids": [c.row.id for c in candidates],
                    "candidates": [candidate_summary(c) for c in candidates],
                    **({"terms": keyword.terms(value)} if route == "keyword" else {}),
                }
            except TracePersistenceError:
                raise
            except Exception as exc:  # noqa: BLE001 - per-route failover with sanitized diagnostics
                info["fallbacks"].append({"stage": key, "error_type": type(exc).__name__})
    info["recall_latency_ms"] = (time.perf_counter() - started) * 1000
    fused = fuse(lanes, settings.RAG_RRF_K)[:pool]
    info["fusion"] = [
        {**candidate_summary(c), "score": c.rrf_score, "ranks": c.ranks} for c in fused
    ]
    ranked = rerank(query, fused, info, trace)
    if selected_context_mode == "relation":
        ranked = coverage_order(ranked, part_indexes, info)
    info["rerank"] = [{**candidate_summary(c), "score": c.rerank_score} for c in ranked]
    if selected_context_mode == "relation":
        texts, citations = expand_relations(
            session, ranked, scope, tenant_id, allow_all_tenants, top_k, info
        )
    else:
        texts, citations = expand(session, ranked, scope, tenant_id, allow_all_tenants, top_k, info)
        info.update(
            protocol_version="rag-context-v1",
            context_mode="legacy",
            context_bundles=[],
            seed_count=len(citations),
            bundle_count=len(citations),
            segment_count=len(citations),
        )
    info["final_count"] = len(texts)
    if details is not None:
        details.extend(citations)
    return texts


def build_rag_context(
    session: Session,
    query: str,
    knowledge_point: str | None = None,
    top_k: int | None = None,
    tenant_id: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
    template_id: str | None = None,
    provenance: dict[str, Any] | None = None,
    mode: str = "optional",
    scope_mode: ScopeMode | None = None,
    context_mode: str | None = None,
) -> str:
    info = provenance if provenance is not None else {}
    info.update(
        status="disabled" if mode == "off" else "no_match",
        citations=[],
        verification="unverified",
        mode=mode,
    )
    if mode not in {"off", "optional", "required"}:
        raise RagContextRequiredError("RAG mode 只能是 off/optional/required")
    if mode == "off":
        return ""
    citations: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    try:
        snippets = retrieve(
            session,
            query=query,
            knowledge_point=knowledge_point,
            top_k=top_k,
            tenant_id=tenant_id,
            trace_id=trace_id,
            task_id=task_id,
            template_id=template_id,
            details=citations,
            scope_mode=scope_mode,
            diagnostics=diagnostics,
            context_mode=context_mode,
        )
        usable = [
            (text, citation)
            for text, citation in zip(snippets, citations)
            if citation.get("eligible") is True
            or (
                citation.get("similarity") is not None
                and citation["similarity"] >= settings.RAG_MIN_SIMILARITY
            )
        ]
        info["status"] = (
            "hit" if usable else "degraded" if diagnostics.get("fallbacks") else "no_match"
        )
        info["protocol_version"] = diagnostics.get("protocol_version", "rag-context-v1")
        info["context_bundles"] = diagnostics.get("context_bundles", [])
        info["citations"] = delivered_citations([entry for _, entry in usable])
        info["bundle_citations"] = [entry for _, entry in usable]
    except (RagRerankRequiredError, TracePersistenceError):
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 - optional RAG records degradation, required RAG fails closed
        info.update(status="degraded", error_code=type(exc).__name__)
        usable = []
        logger.warning("RAG 降级 trace_id=%s error_type=%s", trace_id, type(exc).__name__)
    info["retrieval"] = diagnostics
    if trace_id:
        record_lifecycle_event(
            trace_id=trace_id,
            stage="rag",
            event=info["status"],
            model="pgvector",
            task_id=task_id,
            template_id=template_id,
            tenant_id=tenant_id,
            success=bool(usable),
            metadata=info,
        )
    if mode == "required" and not usable:
        raise RagContextRequiredError("本次生成要求知识来源，但检索无有效命中或已降级")
    return "\n\n".join(
        (
            text
            if citation.get("protocol_version") == "rag-context-v2"
            else format_source(text, citation)
        )
        for text, citation in usable
    )
