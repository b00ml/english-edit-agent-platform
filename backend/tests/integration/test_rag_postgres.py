"""Real migrated PostgreSQL/pgvector RAG tests; no external model calls.

Each test rolls back its own rows on the existing database. Index plan checks
force planner settings only inside the transaction; they prove index usability,
not default-plan performance or semantic retrieval quality.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import inspect, select, text

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.knowledge_points import load_catalog
from app.rag.retrieval import keyword, vector
from app.rag.retrieval.context import expand, format_source
from app.rag.retrieval.models import Candidate, leaf_predicate
from app.services.knowledge_service import KnowledgeService
from tests.integration.embedding_stub import mock_vector

pytestmark = pytest.mark.integration


@pytest.fixture()
def owner():
    from types import SimpleNamespace

    return SimpleNamespace(
        id="integration",
        role="researcher",
        status="active",
        tenant_id=f"rag-test-{uuid.uuid4().hex}",
    )


@pytest.fixture(autouse=True)
def trace_into_test_transaction(pg_session, monkeypatch):
    # Trace is auxiliary persistence with its own commits. Redirect it into this
    # test transaction rather than leaving fixture metadata in the user's DB.
    from sqlalchemy.orm import sessionmaker

    from app import database
    from app.engine import trace

    connection = pg_session.get_bind()
    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(settings, "TRACE_SINKS", "db")
    trace.reset_sinks()
    yield
    trace.reset_sinks()


def leaf(session, tenant, content, embedding, point="一般现在时", **kwargs):
    row = KnowledgeChunk(
        source_type="教材",
        source_name="Integration source",
        content=content,
        embedding=embedding,
        tenant_id=tenant,
        knowledge_point=point,
        search_text=content.casefold(),
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


def test_actual_alembic_head_extensions_indexes_and_self_foreign_keys(pg_session):
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    assert set(
        pg_session.execute(text("SELECT version_num FROM alembic_version")).scalars()
    ) == set(ScriptDirectory.from_config(config).get_heads())
    extensions = dict(pg_session.execute(text("SELECT extname,extversion FROM pg_extension")).all())
    assert {"vector", "pg_trgm"} <= extensions.keys()
    indexes = dict(
        pg_session.execute(
            text("SELECT indexname,indexdef FROM pg_indexes WHERE tablename='knowledge_chunk'")
        ).all()
    )
    for name in [
        "ix_knowledge_chunk_hnsw",
        "ix_knowledge_chunk_fts",
        "ix_knowledge_chunk_trgm",
        "ix_knowledge_chunk_point_ids",
    ]:
        assert name in indexes
        assert (
            pg_session.execute(
                text(
                    "SELECT indisvalid FROM pg_index JOIN pg_class "
                    "ON pg_class.oid=indexrelid WHERE relname=:name"
                ),
                {"name": name},
            ).scalar()
            is True
        )
    assert "vector_cosine_ops" in indexes["ix_knowledge_chunk_hnsw"]
    foreign_keys = {
        fk["constrained_columns"][0]: fk
        for fk in inspect(pg_session.get_bind()).get_foreign_keys("knowledge_chunk")
    }
    assert foreign_keys["parent_chunk_id"]["options"]["ondelete"] == "CASCADE"
    assert foreign_keys["prev_chunk_id"]["options"]["ondelete"] == "SET NULL"


def test_real_cosine_orders_exact_near_orthogonal_and_ignores_parent(
    pg_session, owner, monkeypatch
):
    monkeypatch.setattr(settings, "RAG_MIN_SIMILARITY", 0.3)
    q = [1.0] + [0.0] * 1023
    exact = leaf(pg_session, owner.tenant_id, "same vector", q)
    near = leaf(pg_session, owner.tenant_id, "near vector", [0.8, 0.6] + [0.0] * 1022)
    leaf(pg_session, owner.tenant_id, "orthogonal", [0.0, 1.0] + [0.0] * 1022)
    leaf(pg_session, owner.tenant_id, "context only", None, chunk_type="parent")
    scope = load_catalog().scope("simple present", "exact")
    results = vector.recall(pg_session, q, scope, owner.tenant_id, False, 30)
    assert [c.row.id for c in results] == [exact.id, near.id]
    assert [c.similarity for c in results] == pytest.approx([1, 0.8])


def test_hnsw_index_real_explain_is_usable_without_claiming_default_performance(pg_session, owner):
    q = [1.0] + [0.0] * 1023
    leaf(pg_session, owner.tenant_id, "index reference", q)
    pg_session.execute(text("SET LOCAL enable_seqscan = off"))
    query = """EXPLAIN (FORMAT JSON) SELECT id FROM knowledge_chunk
        WHERE embedding IS NOT NULL AND (chunk_type IS NULL OR chunk_type != 'parent')
        ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5"""
    plan = pg_session.execute(text(query), {"q": json.dumps(q)}).scalar()
    assert "ix_knowledge_chunk_hnsw" in json.dumps(plan)


@pytest.mark.parametrize("kind", ["fts", "trigram", "tags"])
def test_keyword_and_tag_gin_indexes_real_explain(pg_session, owner, kind):
    leaf(
        pg_session,
        owner.tenant_id,
        "running uniquegrammar rules",
        mock_vector("running rules"),
        knowledge_point_ids=["present_simple"],
    )
    pg_session.execute(text("SET LOCAL enable_seqscan = off"))
    predicates = {
        "fts": (
            "to_tsvector('english'::regconfig,coalesce(search_text,'')) "
            "@@ websearch_to_tsquery('english'::regconfig,'runs')"
        ),
        "trigram": "search_text LIKE '%uniquegrammar%'",
        "tags": "knowledge_point_ids @> '[\"present_simple\"]'::jsonb",
    }
    plan = pg_session.execute(
        text("EXPLAIN (FORMAT JSON) SELECT id FROM knowledge_chunk WHERE " + predicates[kind])
    ).scalar()
    name = {
        "fts": "ix_knowledge_chunk_fts",
        "trigram": "ix_knowledge_chunk_trgm",
        "tags": "ix_knowledge_chunk_point_ids",
    }[kind]
    assert name in json.dumps(plan)


def test_actual_english_stemming_cjk_symbols_numbers_and_literal_escaping(pg_session, owner):
    row = leaf(
        pg_session,
        owner.tenant_id,
        "running 第三人称 -ed Unit-12 literal_percent%",
        mock_vector("running"),
    )
    scope = load_catalog().scope(None, "exact")
    for query in ["runs", "第三人称", "-ed", "12", "literal_percent%"]:
        results = keyword.recall(pg_session, query, scope, owner.tenant_id, False, 30)
        assert row.id in [c.row.id for c in results]
    assert keyword.recall(pg_session, "%_", scope, owner.tenant_id, False, 30) == []


def test_jsonb_multitag_alias_scope_and_tenant_null_pg_predicates(pg_session, owner):
    service = KnowledgeService(pg_session)
    service.upload_text(
        "subject verb agreement present rules",
        "教材",
        "Grammar",
        "past simple",
        None,
        owner,
        knowledge_points=["subject-verb agreement"],
    )
    row = pg_session.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == owner.tenant_id, leaf_predicate())
    ).one()
    assert row.knowledge_point_ids == ["past_simple", "subject_verb_agreement"]
    details = []
    results = retriever.retrieve(
        pg_session,
        "agreement",
        knowledge_point="第三人称单数",
        tenant_id=owner.tenant_id,
        details=details,
    )
    assert results and details
    assert (
        retriever.retrieve(
            pg_session, "agreement", knowledge_point="现在时", tenant_id=owner.tenant_id
        )
        == []
    )
    assert (
        retriever.retrieve(pg_session, "agreement", knowledge_point="第三人称单数", tenant_id=None)
        == []
    )
    assert (
        retriever.retrieve(
            pg_session, "agreement", knowledge_point="第三人称单数", tenant_id="other-tenant"
        )
        == []
    )


def test_real_parent_child_neighbors_and_return_budget(pg_session, owner, monkeypatch):
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "legacy")
    service = KnowledgeService(pg_session)
    service.upload_text(
        "# Grammar\n\n" + "agreement rule subject verb. " * 200,
        "教材",
        "Grammar",
        "现在时",
        None,
        owner,
    )
    doc = pg_session.scalars(
        select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == owner.tenant_id)
    ).one()
    parent = pg_session.scalars(
        select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == doc.id, KnowledgeChunk.chunk_type == "parent"
        )
    ).first()
    assert parent and parent.embedding is None
    children = pg_session.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.parent_chunk_id == parent.id)
    ).all()
    scope = load_catalog().scope("simple present", "exact")
    texts, citations = expand(
        pg_session,
        [Candidate(row, similarity=0.9) for row in children],
        scope,
        owner.tenant_id,
        False,
        3,
    )
    assert len(texts) == 1 and citations[0]["chunk_id"] == parent.id
    assert set(citations[0]["matched_child_ids"]) == {row.id for row in children}
    assert citations[0]["content"] == doc.normalized_text[parent.content_start : parent.content_end]
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 300)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", 280)
    texts, citations = expand(
        pg_session, [Candidate(children[0], similarity=0.9)], scope, owner.tenant_id, False, 3
    )
    wrapped = "\n\n".join(format_source(value, info) for value, info in zip(texts, citations))
    assert texts and len(wrapped) <= 300 and len(wrapped.encode()) <= 280
    assert citations[0]["truncated"]


def test_recall_uses_postgres_not_reference_and_sql_fallback_is_not_masked(pg_session, owner):
    KnowledgeService(pg_session).upload_text(
        "agreement grammar word", "教材", "Grammar", "现在时", None, owner
    )
    info = {}
    citations = []
    result = retriever.retrieve(
        pg_session,
        "agreement",
        knowledge_point="simple present",
        tenant_id=owner.tenant_id,
        diagnostics=info,
        details=citations,
    )
    assert result and citations
    assert info["vector_backend"] == "postgres_pgvector"
    assert info["keyword_backend"] == "postgres_fts_literal"
    assert not info[
        "fallbacks"
    ], "Real SQL errors must fail this regression even if the other lane succeeds"
    assert any(key.startswith("vector:") for key in info["lanes"])
    assert any(key.startswith("keyword:") for key in info["lanes"])


def test_failed_real_sql_lane_savepoint_recovers_and_keyword_supplies_required(
    pg_session, owner, monkeypatch
):
    KnowledgeService(pg_session).upload_text(
        "keyword grammar rule", "教材", "Grammar", "现在时", None, owner
    )

    def sql_error(*args, **kwargs):
        pg_session.execute(text("SELECT definitely_missing_rag_test_column FROM knowledge_chunk"))

    monkeypatch.setattr(vector, "recall", sql_error)
    provenance = {}
    context = retriever.build_rag_context(
        pg_session,
        "keyword",
        knowledge_point="simple present",
        tenant_id=owner.tenant_id,
        mode="required",
        provenance=provenance,
    )
    assert context and provenance["retrieval"]["fallbacks"]
    assert any(key.startswith("keyword:") for key in provenance["retrieval"]["lanes"])
    assert pg_session.execute(text("SELECT 1")).scalar() == 1


def test_partial_batch_no_pg_document_written(pg_session, owner, monkeypatch):
    monkeypatch.setattr("app.rag.indexer.embed_texts", lambda *args, **kwargs: [])
    from app.errors import InvalidKnowledgeError

    with pytest.raises(InvalidKnowledgeError):
        KnowledgeService(pg_session).upload_text("rule", "教材", "Grammar", "现在时", None, owner)
    assert (
        pg_session.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == owner.tenant_id)
        ).all()
        == []
    )


def test_parent_neighbor_fk_delete_cleanup_on_postgres(pg_session, owner):
    service = KnowledgeService(pg_session)
    service.upload_text("rule " * 700, "教材", "Grammar", "现在时", None, owner)
    children = pg_session.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == owner.tenant_id, leaf_predicate())
    ).all()
    for child in children:
        service.delete_knowledge(child.id, owner)
    assert (
        pg_session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == owner.tenant_id)
        ).all()
        == []
    )
    assert (
        pg_session.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == owner.tenant_id)
        ).all()
        == []
    )


def test_pg_cjk_bigrams_match_free_question_with_tenant_parent_and_literal_safety(
    pg_session, owner, monkeypatch
):
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "bigram")
    monkeypatch.setattr(settings, "RAG_KEYWORD_MAX_TERMS", 32)
    correct = leaf(pg_session, owner.tenant_id, "连系动词必须与表语构成系表结构", None)
    parent = leaf(pg_session, owner.tenant_id, correct.content, None, chunk_type="parent")
    foreign = leaf(pg_session, str(uuid.uuid4()), correct.content, None)
    scope = load_catalog().scope(None, "exact")
    query = "连系动词为什么需要表语？"
    results = keyword.recall(pg_session, query, scope, owner.tenant_id, False, 30)
    assert [c.row.id for c in results] == [correct.id]
    assert parent.id not in [c.row.id for c in results] and foreign.id not in [
        c.row.id for c in results
    ]
    assert keyword.recall(pg_session, "%_", scope, owner.tenant_id, False, 30) == []
    assert (
        keyword.recall(pg_session, query, scope, owner.tenant_id, False, 30, document_ids=[]) == []
    )
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "legacy")
    assert keyword.recall(pg_session, query, scope, owner.tenant_id, False, 30) == []


def test_pg_unscoped_hybrid_lexical_lane_recovers_fourth_vector_with_diagnostics(
    pg_session, owner, monkeypatch
):
    monkeypatch.setattr(settings, "RAG_MIN_SIMILARITY", 0.3)
    monkeypatch.setattr(settings, "RAG_NEIGHBOR_WINDOW", 0)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "bigram")
    monkeypatch.setattr(settings, "RAG_RETRIEVAL_METHOD", "hybrid")
    q = [1.0] + [0.0] * 1023
    monkeypatch.setattr(retriever, "embed_texts", lambda inputs, **kwargs: [q for _ in inputs])
    for phrase, similarity in [("因果关系", 1), ("并列连词", 0.98), ("选择关系", 0.94)]:
        leaf(
            pg_session,
            owner.tenant_id,
            phrase,
            [similarity, (1 - similarity**2) ** 0.5] + [0.0] * 1022,
        )
    correct = leaf(
        pg_session, owner.tenant_id, "连系动词必须与表语构成系表结构", [0.8, 0.6] + [0.0] * 1022
    )
    citations, info = [], {}
    retriever.retrieve(
        pg_session,
        "连系动词为什么需要表语？",
        tenant_id=owner.tenant_id,
        details=citations,
        diagnostics=info,
    )
    assert citations[0]["chunk_id"] == correct.id and not info["fallbacks"]
    assert info["scope"]["canonical_name"] is None
    assert info["lanes"]["vector:0"]["chunk_ids"].index(correct.id) == 3
    assert info["lanes"]["keyword:0"]["chunk_ids"] == [correct.id]
    assert info["rerank"][0]["chunk_id"] == correct.id
