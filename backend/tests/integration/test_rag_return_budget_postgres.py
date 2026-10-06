"""Top-5 default / Top-8 override on the existing migrated PG, rollback-only fixtures."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.rag import retriever
from tests.test_rag_return_budget import seed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("budget", [None, 8])
def test_real_pg_respects_requested_budget_and_preserves_tenant_scope(
    pg_session: Session, monkeypatch: pytest.MonkeyPatch, budget: int | None
) -> None:
    tenant = "topk-" + uuid.uuid4().hex
    own = seed(pg_session, tenant)
    foreign = seed(pg_session, tenant + "-foreign", count=2)
    monkeypatch.setattr(settings, "RAG_TOP_K", 5)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_RETRIEVAL_METHOD", "vector")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_CANDIDATE_POOL", 30)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 8192)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", None)
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )
    details, info = [], {}
    texts = retriever.retrieve(
        pg_session,
        "independent rules",
        tenant_id=tenant,
        top_k=budget,
        details=details,
        diagnostics=info,
    )
    assert len(texts) == len(details) == (budget or 5)
    assert info["vector_backend"] == "postgres_pgvector"
    assert info["requested_top_k"] == (budget or 5) and info["candidate_pool"] == 30
    assert info["context_max_chars"] == 8192 and len("\n\n".join(texts)) <= 8192
    assert info["lanes"]["vector:0"]["count"] == 10
    assert {c["chunk_id"] for c in details} <= {r.id for r in own}
    assert not {c["chunk_id"] for c in details}.intersection(r.id for r in foreign)


def test_real_pg_expanded_budget_returns_more_than_old_8192_chars(
    pg_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = "large-context-" + uuid.uuid4().hex
    rows = seed(pg_session, tenant)
    for i, row in enumerate(rows):
        row.content = f"Independent source {i}. " + "An attributed example. " * 100
    pg_session.flush()
    monkeypatch.setattr(settings, "RAG_TOP_K", 8)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_RETRIEVAL_METHOD", "vector")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_CANDIDATE_POOL", 30)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 32768)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", None)
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )
    info = {}
    texts = retriever.retrieve(pg_session, "examples", tenant_id=tenant, diagnostics=info)
    assert len(texts) == 8 and 8192 < len("\n\n".join(texts)) <= 32768
    assert info["context_chars"] == len("\n\n".join(texts))
    assert info["requested_top_k"] == 8 and info["context_max_chars"] == 32768
