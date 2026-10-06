"""Requested Top-5/Top-8 budgets, unchanged scope/candidate limits and immutable gold."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models import KnowledgeChunk
from app.rag import retriever
from scripts import rag_eval


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_TOP_K", 5)
    monkeypatch.setattr(settings, "RAG_CANDIDATE_POOL", 30)
    monkeypatch.setattr(settings, "RAG_RETRIEVAL_METHOD", "vector")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_NEIGHBOR_WINDOW", 0)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 8192)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", None)
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )


def seed(db: Session, tenant: str = "owner", count: int = 10) -> list[KnowledgeChunk]:
    rows = [
        KnowledgeChunk(
            source_type="教材",
            source_name="Return budget fixture",
            content=f"Rule number {i}. Its independently cited example.",
            tenant_id=tenant,
            chunk_type="single",
            embedding=[1.0, i * 0.01] + [0.0] * 1022,
        )
        for i in range(count)
    ]
    db.add_all(rows)
    db.flush()
    return rows


def test_configuration_defaults_to_five_and_explicit_eight_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAG_TOP_K", raising=False)
    assert Settings(_env_file=None).RAG_TOP_K == 5
    monkeypatch.delenv("RAG_CONTEXT_MAX_CHARS", raising=False)
    assert Settings(_env_file=None).RAG_CONTEXT_MAX_CHARS == 32768
    assert Settings(_env_file=None, RAG_TOP_K=8).RAG_TOP_K == 8


@pytest.mark.parametrize("value", [0, 11, 31])
def test_invalid_configuration_return_budget_is_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, RAG_TOP_K=value)


@pytest.mark.parametrize("mode", ["legacy", "relation"])
@pytest.mark.parametrize("budget", [None, 5, 8])
def test_return_budget_keeps_candidate_pool_scope_and_context_bound(
    db: Session, mode: str, budget: int | None
) -> None:
    own = seed(db)
    foreign = seed(db, tenant="foreign", count=2)
    details, info = [], {}
    texts = retriever.retrieve(
        db,
        "independent rules",
        top_k=budget,
        tenant_id="owner",
        context_mode=mode,
        details=details,
        diagnostics=info,
    )
    effective = budget or 5
    assert len(texts) == len(details) == effective
    assert info["requested_top_k"] == effective and info["candidate_pool"] == 30
    assert info["context_max_chars"] == 8192 and info["context_token_limit"] is None
    assert len("\n\n".join(texts)) <= 8192
    assert {c["chunk_id"] for c in details} <= {r.id for r in own}
    assert not {c["chunk_id"] for c in details}.intersection(r.id for r in foreign)
    assert info["lanes"]["vector:0"]["count"] == 10


def test_top_eight_does_not_relax_hard_context_budget(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = seed(db)
    for row in rows:
        row.content = "An independent example. " * 50
    db.flush()
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 512)
    details, info = [], {}
    texts = retriever.retrieve(
        db, "examples", tenant_id="owner", top_k=8, details=details, diagnostics=info
    )
    assert texts and len(texts) < 8 and len("\n\n".join(texts)) <= 512
    assert info["requested_top_k"] == 8 and info["context_max_chars"] == 512


def test_api_uses_current_config_when_top_k_omitted_and_honors_eight(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import router
    from app.database import get_db
    from app.security import get_current_user

    seed(db)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="owner", tenant_id="owner", role="researcher", status="active"
    )
    with TestClient(application) as client:
        assert client.get("/api/knowledge/points").json()["defaults"]["top_k"] == 5
        response = client.get("/api/knowledge/retrieve", params={"query": "rules"})
        assert response.status_code == 200 and len(response.json()["bundles"]) == 5
        assert response.json()["diagnostics"]["requested_top_k"] == 5
        # Query defaults must not be frozen when the router was imported.
        monkeypatch.setattr(settings, "RAG_TOP_K", 8)
        response = client.get("/api/knowledge/retrieve", params={"query": "rules"})
        assert response.status_code == 200 and len(response.json()["bundles"]) == 8
        response = client.get("/api/knowledge/retrieve", params={"query": "rules", "top_k": 5})
        assert response.status_code == 200 and len(response.json()["bundles"]) == 5
        for invalid in [0, 11, "bad"]:
            assert (
                client.get(
                    "/api/knowledge/retrieve", params={"query": "rules", "top_k": invalid}
                ).status_code
                == 422
            )


@pytest.mark.parametrize("budget", [None, 5, 8])
def test_evaluator_budget_override_preserves_gold_and_scores_delivered_budget(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int | None
) -> None:
    case = rag_eval.Case(
        id="budget",
        query="rule",
        top_k=3,
        relevant=[{"document_id": "doc", "pages": [1], "required_terms": ["target"]}],
    )
    gold, output = tmp_path / "gold.json", tmp_path / "report.json"
    gold.write_text(json.dumps([case.model_dump()]), encoding="utf-8")
    original = gold.read_bytes()
    argv = ["eval", "--gold", str(gold), "--output", str(output), "--run-id", "budget"]
    if budget:
        argv.extend(["--top-k", str(budget)])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(rag_eval, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(rag_eval, "corpus_manifest", lambda *args: {"sha256": "unchanged"})
    calls = []

    def retrieve(session: Session, query: str, **kwargs) -> list[str]:
        calls.append(kwargs["top_k"])
        kwargs["details"].extend(
            {
                "document_id": "doc",
                "page_no": 1,
                "chunk_id": f"leaf-{i}",
                "content": "target" if i == 3 else "unrelated",
            }
            for i in range(kwargs["top_k"])
        )
        return []

    monkeypatch.setattr(rag_eval, "retrieve", retrieve)
    assert rag_eval.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert calls == [budget or 3]
    assert report["top_k_override"] == budget
    assert report["gold_sha256"] == hashlib.sha256(original).hexdigest()
    assert gold.read_bytes() == original and report["results"][0]["case"]["top_k"] == 3
    assert report["results"][0]["effective_top_k"] == (budget or 3)
    assert report["metrics"]["context_complete"] == (1.0 if budget else 0.0)


@pytest.mark.parametrize("budget", ["0", "11", "bad"])
def test_evaluator_invalid_budget_fails_before_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval",
            "--gold",
            str(tmp_path / "missing.json"),
            "--output",
            str(tmp_path / "report.json"),
            "--top-k",
            budget,
        ],
    )
    monkeypatch.setattr(rag_eval, "retrieve", lambda *a, **kw: pytest.fail("no paid call"))
    with pytest.raises(SystemExit) as error:
        rag_eval.main()
    assert error.value.code == 2


@pytest.mark.parametrize("budget", [5, 8])
def test_expanded_context_budget_can_deliver_over_8192_without_exceeding_new_limit(
    db: Session, monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    rows = seed(db)
    for i, row in enumerate(rows):
        row.content = f"Independent source {i}. " + "An independently attributed example. " * 90
    db.flush()
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 32768)
    details, info = [], {}
    texts = retriever.retrieve(
        db, "examples", tenant_id="owner", top_k=budget, details=details, diagnostics=info
    )
    assert len(texts) == budget
    assert 8192 < len("\n\n".join(texts)) <= 32768
    assert info["context_chars"] == len("\n\n".join(texts))
    assert info["context_max_chars"] == 32768 and info["requested_top_k"] == budget
    assert info["context_token_limit"] is None


def test_expanded_char_budget_does_not_disable_explicit_byte_guard(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = seed(db)
    for row in rows:
        row.content = "跨页定义和有来源的例句。" * 400
    db.flush()
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 32768)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", 4096)
    info = {}
    texts = retriever.retrieve(db, "examples", tenant_id="owner", top_k=8, diagnostics=info)
    assert texts and len("\n\n".join(texts).encode("utf-8")) <= 4096
    assert info["context_token_limit"] == 4096 and info["context_max_chars"] == 32768
