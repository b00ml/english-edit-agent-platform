"""RAG P1 DEF regressions: all providers mocked; SQLite is a labelled reference backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.errors import (
    InvalidKnowledgeError,
    RagContextRequiredError,
    RagRerankRequiredError,
    TracePersistenceError,
)
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.knowledge_points import CatalogData, KnowledgeCatalog, load_catalog
from app.rag.retrieval import expansion, keyword, rerank, vector
from app.rag.retrieval.context import expand, format_source
from app.rag.retrieval.fusion import fuse
from app.rag.retrieval.models import Candidate, predicates
from app.services.knowledge_service import KnowledgeService


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "legacy")
    # This suite fixes the pre-STR5 leaf layout; new defaults have separate contracts.
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "legacy")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda inputs, **kw: [[1.0] * 1024 for _ in inputs]
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda inputs, **kw: [[1.0] * 1024 for _ in inputs]
    )
    monkeypatch.setattr(expansion, "record_trace", lambda **kw: None)
    monkeypatch.setattr(rerank, "record_trace", lambda **kw: None)
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "aliases")


def user(tenant="a", role="researcher"):
    return SimpleNamespace(id="human", tenant_id=tenant, role=role, status="active")


def ingest(db, text="Rule. " * 600, point="一般现在时", tenant="a", tags=None):
    service = KnowledgeService(db)
    service.upload_text(text, "教材", "Grammar", point, None, user(tenant), tags)
    return db.query(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc()).all()[-1]


def leaves(db):
    return (
        db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.chunk_type != "parent")
        .order_by(KnowledgeChunk.chunk_index)
        .all()
    )


def test_parent_child_rows_vectors_lineage_and_atomic_document_snapshot(db):
    doc = ingest(db)
    children = leaves(db)
    parents = db.query(KnowledgeChunk).filter_by(chunk_type="parent").all()
    assert parents and children
    assert all(row.embedding is None for row in parents)
    assert all(row.embedding is not None for row in children)
    for child in children:
        assert child.content == doc.normalized_text[child.content_start : child.content_end]
        if child.parent_chunk_id:
            parent = db.get(KnowledgeChunk, child.parent_chunk_id)
            assert (
                parent.content_start
                <= child.content_start
                < child.content_end
                <= parent.content_end
            )
            assert parent.tenant_id == child.tenant_id
    assert children[0].prev_chunk_id is None and children[-1].next_chunk_id is None
    assert children[0].next_chunk_id == children[1].id
    assert KnowledgeService(db).list_knowledge(None, 1, 100, user())["total"] == len(children)


def test_short_document_remains_single_layer(db):
    ingest(db, "short grammar rule")
    assert db.query(KnowledgeChunk).count() == 1
    assert db.query(KnowledgeChunk).one().chunk_type == "single"


def test_child_recall_returns_deduplicated_parent_with_source_slice(db):
    doc = ingest(db)
    parent = db.query(KnowledgeChunk).filter_by(chunk_type="parent").first()
    children = db.query(KnowledgeChunk).filter_by(parent_chunk_id=parent.id).all()
    scope = load_catalog().scope("现在时", "exact")
    texts, citations = expand(
        db, [Candidate(c, similarity=0.9) for c in children], scope, "a", False, 5
    )
    assert len(texts) == len(citations) == 1
    assert citations[0]["chunk_id"] == parent.id
    assert set(citations[0]["matched_child_ids"]) == {c.id for c in children}
    assert citations[0]["content"] == doc.normalized_text[parent.content_start : parent.content_end]
    assert citations[0]["context_role"] == "parent"


def test_parent_groups_do_not_cross_sections(db):
    ingest(db, "# One\n\n" + "Rule one. " * 150 + "\n\n# Two\n\n" + "Rule two. " * 150)
    for parent in db.query(KnowledgeChunk).filter_by(chunk_type="parent"):
        children = db.query(KnowledgeChunk).filter_by(parent_chunk_id=parent.id).all()
        assert all(c.section_path == parent.section_path for c in children)
        assert not ("# One" in parent.content and "# Two" in parent.content)


def test_parent_and_neighbor_expansion_cannot_cross_tenant_or_document(db):
    ingest(db)
    own = leaves(db)[0]
    foreign = KnowledgeChunk(
        source_type="教材",
        source_name="Foreign",
        content="secret",
        tenant_id="b",
        embedding=None,
        chunk_type="parent",
        document_id=own.document_id,
        knowledge_point="一般现在时",
        knowledge_point_ids=own.knowledge_point_ids,
        section_path=own.section_path,
        content_start=0,
        content_end=6,
    )
    db.add(foreign)
    db.flush()
    own.parent_chunk_id = foreign.id
    scope = load_catalog().scope("一般现在时", "exact")
    texts, citations = expand(db, [Candidate(own, similarity=0.9)], scope, "a", False, 3)
    assert "secret" not in "".join(texts)
    assert all(c["chunk_id"] != foreign.id for c in citations)


def test_neighbors_are_bounded_and_overlaps_not_repeated(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    doc = ingest(db)
    children = leaves(db)
    texts, info = expand(
        db,
        [Candidate(children[1], similarity=0.9)],
        load_catalog().scope("一般现在时", "exact"),
        "a",
        False,
        3,
    )
    assert len(texts) <= 3
    ranges = []
    for citation in info:
        start, end = citation["content_start"], citation["content_end"]
        assert citation["content"] == doc.normalized_text[start:end]
        assert all(end <= a or start >= b for a, b in ranges)
        ranges.append((start, end))


def test_character_and_byte_budgets_include_source_wrappers(db, monkeypatch):
    doc = ingest(db)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 300)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", 280)
    details = []
    texts = retriever.retrieve(db, "rule", knowledge_point="现在时", tenant_id="a", details=details)
    wrapped = "\n\n".join(format_source(text, citation) for text, citation in zip(texts, details))
    assert len(wrapped) <= 300 and len(wrapped.encode()) <= 280
    assert any(c["truncated"] for c in details)
    for citation in details:
        assert (
            citation["content"]
            == doc.normalized_text[citation["content_start"] : citation["content_end"]]
        )


def test_last_child_delete_removes_orphan_parent_and_document(db):
    ingest(db)
    service = KnowledgeService(db)
    for row in list(leaves(db)):
        service.delete_knowledge(row.id, user())
    assert db.query(KnowledgeChunk).count() == db.query(KnowledgeDocument).count() == 0


def test_alias_ingestion_multitags_canonicalized(db):
    ingest(db, "verb rule", "simple present", tags=["subject-verb agreement"])
    row = db.query(KnowledgeChunk).one()
    assert row.knowledge_point == "一般现在时"
    assert set(row.knowledge_point_ids) == {"present_simple", "subject_verb_agreement"}


@pytest.mark.parametrize("label", ["一般现在时", "现在时", "SIMPLE PRESENT", " present   simple "])
def test_canonical_and_alias_resolution(label):
    scope = load_catalog().scope(label, "exact")
    assert scope.ids == ["present_simple"] and scope.canonical_name == "一般现在时"


@pytest.mark.parametrize(
    "mode,contained",
    [
        ("ancestor", {"grammar", "tenses", "present_simple"}),
        ("related", {"present_simple", "subject_verb_agreement"}),
        ("semantic", set()),
    ],
)
def test_scopes(mode, contained):
    assert set(load_catalog().scope("现在时", mode).ids) == contained


def test_descendant_scope_includes_children_only():
    scope = load_catalog().scope("时态", "descendant")
    assert {"tenses", "present_simple", "present_continuous", "past_simple"} <= set(scope.ids)
    assert "subject_verb_agreement" not in scope.ids


def test_unknown_point_stays_literal_without_broadening():
    scope = load_catalog().scope("Custom Topic", "exact")
    assert scope.labels == ["custom topic"] and scope.ids == []


@pytest.mark.parametrize(
    "points",
    [
        [{"id": "a", "canonical_name": "A", "parent_id": "a"}],
        [{"id": "a", "canonical_name": "A", "parent_id": "missing"}],
        [
            {"id": "a", "canonical_name": "A", "aliases": ["same"]},
            {"id": "b", "canonical_name": "B", "aliases": ["same"]},
        ],
        [{"id": "a", "canonical_name": "A"}, {"id": "a", "canonical_name": "B"}],
        [{"id": "a", "canonical_name": "A", "related": ["missing"]}],
    ],
)
def test_invalid_catalog_rejected(points):
    with pytest.raises(InvalidKnowledgeError):
        KnowledgeCatalog(CatalogData.model_validate({"version": 1, "points": points}), "hash")


def test_disabled_point_rejected():
    catalog = KnowledgeCatalog(
        CatalogData.model_validate(
            {"version": 1, "points": [{"id": "a", "canonical_name": "A", "status": "disabled"}]}
        ),
        "hash",
    )
    with pytest.raises(InvalidKnowledgeError):
        catalog.scope("A", "exact")


def test_exact_alias_scope_does_not_admit_related_or_foreign_tenant(db):
    ingest(db, "present rule", "simple present", "a")
    ingest(db, "past rule", "past simple", "a")
    ingest(db, "foreign present rule", "现在时", "b")
    details = []
    text = retriever.retrieve(
        db, "simple present", knowledge_point="现在时", tenant_id="a", details=details
    )
    assert text and "foreign" not in " ".join(text) and "past rule" not in " ".join(text)
    assert all(c["document_id"] for c in details)


def test_legacy_alias_label_can_be_recalled_without_reembedding(db):
    legacy = KnowledgeChunk(
        source_type="教材",
        source_name="Legacy",
        content="verb rule",
        knowledge_point="simple present",
        tenant_id="a",
        embedding=[1.0] * 1024,
    )
    db.add(legacy)
    db.commit()
    details = []
    assert retriever.retrieve(db, "rule", knowledge_point="现在时", tenant_id="a", details=details)
    assert details[0]["legacy"]


def test_multitag_secondary_point_recall(db):
    ingest(db, "agreement rule", "现在时", tags=["主谓一致"])
    assert retriever.retrieve(
        db, "agreement", knowledge_point="subject-verb agreement", tenant_id="a"
    )


def test_pg_scope_sql_contains_tenant_tags_and_same_document():
    scope = load_catalog().scope("现在时", "exact")

    class Session:
        pass

    stmt = select(KnowledgeChunk).where(*predicates(scope, None, False, Session(), ["doc"]))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "tenant_id IS NULL" in sql and "document_id IN" in sql and "@>" in sql


def test_vector_candidates_ignore_model_mismatch_parent_and_invalid_dimension(db):
    ingest(db, "present rule")
    row = db.query(KnowledgeChunk).one()
    row.embedding_model = "different-model"
    db.commit()
    scope = load_catalog().scope("现在时", "exact")
    assert vector.recall(db, [1.0] * 1024, scope, "a", False, 30) == []
    with pytest.raises(InvalidKnowledgeError):
        vector.recall(db, [0.0] * 1024, scope, "a", False, 30)


def test_rrf_id_dedup_and_exact_formula():
    a = KnowledgeChunk(id="a", content="a")
    b = KnowledgeChunk(id="b", content="b")
    results = fuse(
        {
            "vector:0": [Candidate(a, similarity=0.9), Candidate(a), Candidate(b, similarity=0.8)],
            "keyword:0": [Candidate(b, keyword_score=1), Candidate(a, keyword_score=0.5)],
        },
        60,
    )
    merged = {c.row.id: c for c in results}
    assert len(results) == 2 and merged["a"].rrf_score == pytest.approx(1 / 61 + 1 / 62)
    assert merged["a"].ranks == {"vector:0": 1, "keyword:0": 2}


def test_keyword_only_when_embedding_fails_and_required_context_can_succeed(db, monkeypatch):
    ingest(db, "He does homework. Grammar rule.")

    def failure(*args, **kwargs):
        raise RuntimeError("secret-provider-error")

    monkeypatch.setattr(retriever, "embed_texts", failure)
    provenance = {}
    context = retriever.build_rag_context(
        db, "does", knowledge_point="现在时", tenant_id="a", mode="required", provenance=provenance
    )
    assert "does" in context and provenance["status"] == "hit"
    assert provenance["retrieval"]["fallbacks"] and "secret-provider-error" not in str(provenance)
    assert provenance["citations"][0]["similarity"] is None


def test_keyword_failure_isolated_with_vector_fallback(db, monkeypatch):
    ingest(db, "relevant rule")
    monkeypatch.setattr(
        keyword, "recall", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    diag = {}
    assert retriever.retrieve(db, "rule", tenant_id="a", diagnostics=diag)
    assert any(e["stage"].startswith("keyword") for e in diag["fallbacks"])


def test_both_recall_routes_fail_required_closed(db, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(retriever, "embed_texts", failure)
    monkeypatch.setattr(keyword, "recall", failure)
    with pytest.raises(RagContextRequiredError):
        retriever.build_rag_context(db, "rule", mode="required")


def test_literal_keyword_precision_symbols_cjk_and_numbers(db):
    ingest(db, "第三人称单数 does / -ed / Unit-12")
    scope = load_catalog().scope(None, "exact")
    for query in ["第三人称单数", "does", "-ed", "12"]:
        assert keyword.recall(db, query, scope, "a", False, 30)


def test_pg_keyword_sql_uses_fts_and_parameterized_literals():
    statements = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def execute(self, stmt):
            statements.append(stmt)
            return Result()

    keyword.recall(Session(), "do_%", load_catalog().scope(None, "exact"), None, False, 30)
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "websearch_to_tsquery" in sql and "@@" in sql and "ESCAPE" in sql
    assert "do_%" not in sql


def setup_reranker(monkeypatch):
    monkeypatch.setattr(settings, "RAG_RERANK_URL", "https://rerank.invalid/rerank")
    monkeypatch.setattr(settings, "RAG_RERANK_MODEL", "configured-model")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "optional")


def test_reranker_validates_full_indices_then_applies_threshold(db, monkeypatch):
    setup_reranker(monkeypatch)
    ingest(db, "rule " * 150)
    candidates = [Candidate(r) for r in leaves(db)]
    monkeypatch.setattr(
        rerank,
        "_request",
        lambda *args: {
            "results": [{"index": i, "relevance_score": i / 10} for i in range(len(candidates))]
        },
    )
    monkeypatch.setattr(settings, "RAG_RERANK_THRESHOLD", 0.1)
    diag = {}
    result = rerank.rerank("rule", candidates, diag, {})
    assert all(c.rerank_score >= 0.1 for c in result) and diag["rerank_status"] == "applied"


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "relevance_score": float("nan")}],
        [{"index": 99, "relevance_score": 0.9}],
        [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}],
    ],
)
def test_invalid_rerank_optional_fallback_and_required_closed(monkeypatch, results):
    setup_reranker(monkeypatch)
    monkeypatch.setattr(rerank, "_request", lambda *args: {"results": results})
    original = [
        Candidate(KnowledgeChunk(id="a", content="rule")),
        Candidate(KnowledgeChunk(id="b", content="rule")),
    ]
    assert rerank.rerank("rule", original, {}, {}) == original
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "required")
    with pytest.raises(RagRerankRequiredError):
        rerank.rerank("rule", original, {}, {})


def test_required_rerank_failure_not_swallowed_by_optional_rag(db, monkeypatch):
    ingest(db, "rule")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "required")
    monkeypatch.setattr(settings, "RAG_RERANK_URL", "")
    with pytest.raises(RagRerankRequiredError):
        retriever.build_rag_context(db, "rule", tenant_id="a", mode="optional")


def test_rerank_trace_unknown_billing_and_sanitized_error(monkeypatch):
    setup_reranker(monkeypatch)
    traces = []
    monkeypatch.setattr(rerank, "record_trace", lambda **kw: traces.append(kw))

    def failure(*args):
        raise RuntimeError("SECRET_API_KEY")

    monkeypatch.setattr(rerank, "_request", failure)
    rerank.rerank(
        "rule", [Candidate(KnowledgeChunk(id="a", content="rule"))], {}, {"trace_id": "t"}
    )
    assert (
        len(traces) == 1 and traces[0]["success"] is False and traces[0]["usage_reported"] is False
    )
    assert "SECRET_API_KEY" not in str(traces)


def test_alias_expansion_bounded_and_original_retained():
    diag = {}
    catalog = load_catalog()
    queries = expansion.expand_queries(
        "simple present", catalog.scope("现在时", "exact"), catalog, diag, {}
    )
    assert queries[0] == "simple present" and "一般现在时" in queries
    assert len(queries) <= settings.RAG_QUERY_EXPANSION_MAX


def test_off_expansion_never_calls_llm(monkeypatch):
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(
        expansion,
        "llm_queries",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    catalog = load_catalog()
    assert expansion.expand_queries(
        "simple present", catalog.scope(None, "exact"), catalog, {}, {}
    ) == ["simple present"]


def test_llm_expansion_failure_reverts_to_original_aliases(monkeypatch):
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "llm")
    monkeypatch.setattr(
        expansion, "llm_queries", lambda *args: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    catalog = load_catalog()
    diag = {}
    queries = expansion.expand_queries(
        "simple present", catalog.scope("现在时", "exact"), catalog, diag, {}
    )
    assert queries[0] == "simple present" and diag["expansion_status"] == "degraded"
    assert "secret" not in str(diag)


def test_llm_queries_cannot_broaden_knowledge_scope(db, monkeypatch):
    ingest(db, "present rule", "现在时")
    ingest(db, "past rule", "一般过去时")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "llm")
    monkeypatch.setattr(expansion, "llm_queries", lambda *args: ["past rule"])
    texts = retriever.retrieve(db, "explain rule", knowledge_point="simple present", tenant_id="a")
    assert "past rule" not in " ".join(texts)


def test_auxiliary_trace_durability_failure_is_never_optional_fallback(db, monkeypatch):
    ingest(db, "rule")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "optional")
    monkeypatch.setattr(
        rerank,
        "provider_scores",
        lambda *args: (_ for _ in ()).throw(TracePersistenceError("lost")),
    )
    with pytest.raises(TracePersistenceError):
        retriever.build_rag_context(db, "rule", tenant_id="a")


def test_source_name_document_and_section_scope_filters(db):
    doc = ingest(db, "# Grammar\n\nrule " * 100)
    assert retriever.retrieve(
        db, "rule", tenant_id="a", document_ids=[doc.id], source_name="Grammar"
    )
    assert retriever.retrieve(db, "rule", tenant_id="a", document_ids=[]) == []
    assert retriever.retrieve(db, "rule", tenant_id="a", source_name="different") == []


def test_catalog_api_and_retrieve_diagnostics_contract(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router
    from app.database import get_db
    from app.security import get_current_user

    ingest(db, "He does homework", "现在时")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user()
    client = TestClient(app)
    points = client.get("/api/knowledge/points")
    assert points.status_code == 200 and points.json()["catalog_hash"]
    response = client.get(
        "/api/knowledge/retrieve",
        params={"query": "does", "knowledge_point": "simple present", "scope_mode": "exact"},
    )
    assert (
        response.status_code == 200
        and response.json()["diagnostics"]["scope"]["canonical_id"] == "present_simple"
    )
    assert response.json()["citations"]


@pytest.mark.parametrize(
    "payload",
    [
        "bad-json",
        '{"queries":[123]}',
        '{"queries":[""]}',
        '{"queries":["a"],"scope_mode":"semantic"}',
    ],
)
def test_llm_provider_structured_validation_and_failed_cost_trace(monkeypatch, payload):
    traces = []
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=10)
    response = SimpleNamespace(
        usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    )
    monkeypatch.setattr(expansion, "_client", lambda: client)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODEL", "configured-query-model")
    monkeypatch.setattr(expansion, "record_trace", lambda **kw: traces.append(kw))
    with pytest.raises(ValueError):
        expansion.llm_queries("rule", load_catalog().scope(None, "exact"), {"trace_id": "t"})
    assert traces[0]["success"] is False and traces[0]["cost"] > 0
    assert traces[0]["prompt_tokens"] == 20


def test_llm_valid_response_bounded_and_prompt_version_stable(monkeypatch):
    import json

    traces = []
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"queries": ["one", "two", "three", "four"]})
                    )
                )
            ],
        )

    monkeypatch.setattr(
        expansion,
        "_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    monkeypatch.setattr(expansion, "record_trace", lambda **kw: traces.append(kw))
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODEL", "configured-query-model")
    scope = load_catalog().scope(None, "exact")
    assert expansion.llm_queries("first", scope, {}) == ["one", "two", "three"]
    expansion.llm_queries("second", scope, {})
    assert traces[0]["prompt_version"] == traces[1]["prompt_version"]
    assert calls[0]["max_tokens"] == settings.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS


def test_large_knowledge_scope_rejected_before_providers(monkeypatch):
    monkeypatch.setattr(settings, "RAG_KNOWLEDGE_SCOPE_MAX", 1)
    with pytest.raises(InvalidKnowledgeError):
        load_catalog().scope("时态", "descendant")


def test_invalid_secondary_tags_rejected_before_embedding(db, monkeypatch):
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    with pytest.raises(InvalidKnowledgeError):
        ingest(db, "rule", tags=[""])
    assert db.query(KnowledgeChunk).count() == 0


def test_vector_query_dimension_mismatch_skips_cloud_and_retains_keyword(db, monkeypatch):
    ingest(db, "a keyword rule")
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 512)
    monkeypatch.setattr(
        retriever,
        "embed_texts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cloud called")),
    )
    diag = {}
    assert retriever.retrieve(db, "keyword", tenant_id="a", diagnostics=diag)
    assert diag["fallbacks"][0]["stage"] == "embedding"


def test_empty_document_scope_never_calls_providers(db, monkeypatch):
    monkeypatch.setattr(
        retriever,
        "embed_texts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    assert retriever.retrieve(db, "rule", document_ids=[]) == []


def test_legacy_unknown_multitag_matches_case_normalized(db):
    ingest(db, "custom rule", None, tags=["Custom Topic"])
    assert retriever.retrieve(db, "custom", knowledge_point="CUSTOM TOPIC", tenant_id="a")


def test_foreign_document_filter_and_null_tenant_never_bypass(db):
    doc = ingest(db, "private rule", tenant="a")
    assert retriever.retrieve(db, "private", tenant_id=None, document_ids=[doc.id]) == []
    assert retriever.retrieve(db, "private", tenant_id="b", document_ids=[doc.id]) == []
    assert retriever.retrieve(
        db, "private", tenant_id=None, document_ids=[doc.id], allow_all_tenants=True
    )


def test_reranker_transport_payload_and_headers_are_configuration_driven(monkeypatch):
    setup_reranker(monkeypatch)
    monkeypatch.setattr(settings, "RAG_RERANK_API_KEY", "private-key")
    captured = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    class Client:
        def __init__(self, **kw):
            captured.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kw):
            captured.append((url, kw))
            return Response()

    monkeypatch.setattr(rerank.httpx, "Client", Client)
    result = rerank._request("rule", ["document"])
    assert result["results"]
    assert captured[1][1]["json"]["model"] == "configured-model"
    assert captured[1][1]["headers"]["Authorization"] == "Bearer private-key"


def test_context_sql_failure_falls_back_to_matched_leaf(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.rag.retrieval import context

    ingest(db)
    child = leaves(db)[0]
    original = db.execute

    def execute(stmt, *args, **kwargs):
        if "knowledge_chunk.id =" in str(stmt):
            raise OperationalError("select", {}, RuntimeError("private connection detail"))
        return original(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)
    diag = {}
    texts, citations = context.expand(
        db,
        [Candidate(child, similarity=0.9)],
        load_catalog().scope("现在时", "exact"),
        "a",
        False,
        3,
        diag,
    )
    assert texts and citations[0]["chunk_id"] == child.id
    assert "private connection" not in str(diag) and diag["fallbacks"]


def test_primary_matches_precede_optional_neighbors(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    ingest(db)
    children = leaves(db)
    first, last = children[0], children[-1]
    texts, citations = expand(
        db,
        [Candidate(first, similarity=0.9), Candidate(last, similarity=0.8)],
        load_catalog().scope("现在时", "exact"),
        "a",
        False,
        2,
    )
    assert {c["chunk_id"] for c in citations} == {first.id, last.id}


def test_candidate_pool_larger_than_returned_top_k(db, monkeypatch):
    ingest(db, "rule " * 300)
    pools = []
    original = vector.recall

    def capture(*args, **kwargs):
        pools.append(args[5])
        return original(*args, **kwargs)

    monkeypatch.setattr(vector, "recall", capture)
    assert retriever.retrieve(db, "rule", tenant_id="a", top_k=1)
    assert pools and pools[0] == settings.RAG_CANDIDATE_POOL


def test_migration_contains_ann_fts_tags_and_safe_downgrade():
    from pathlib import Path

    migration = Path(__file__).parents[1] / "alembic/versions/rag_p1_def.py"
    source = migration.read_text(encoding="utf-8")
    assert "vector_cosine_ops" in source and "gin_trgm_ops" in source and "to_tsvector" in source
    assert "chunk_type='parent' AND embedding IS NULL" in source
    assert "DROP EXTENSION" not in source
