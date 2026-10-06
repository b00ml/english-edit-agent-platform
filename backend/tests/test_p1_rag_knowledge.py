from types import SimpleNamespace

import pytest
from test_p0_quality_events import seed

from app.errors import ContentStateConflictError, InvalidKnowledgeError, RagContextRequiredError
from app.models import ContentItem, KnowledgeChunk
from app.rag import retriever
from app.rag.indexer import index_document
from app.schemas import QualityReviewRequest
from app.services.content_service import ContentService
from app.services.knowledge_service import KnowledgeService
from app.services.quality_service import QualityService
from app.workflow import graph as wf


@pytest.mark.parametrize("tenant", [None, "a"])
def test_retrieval_sql_null_scope_is_explicit_not_global(monkeypatch, tenant):
    statements = []
    monkeypatch.setattr(retriever, "embed_texts", lambda *args, **kwargs: [[1.0] * 1024])

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        def execute(self, statement):
            statements.append(statement)
            return Result()

    retriever.retrieve(Session(), "query", tenant_id=tenant)
    sql = str(statements[0])
    assert (
        "knowledge_chunk.tenant_id IS NULL" in sql
        if tenant is None
        else "knowledge_chunk.tenant_id =" in sql
    )


def test_rag_hit_preserves_chunk_source_hash_and_does_not_assert_fact_verification(monkeypatch):
    traced = []

    def retrieve(*args, **kwargs):
        kwargs["details"].append(
            {"chunk_id": "c1", "source_name": "Grammar", "content_hash": "hash", "similarity": 0.9}
        )
        return ["a grammar rule"]

    monkeypatch.setattr(retriever, "retrieve", retrieve)
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kwargs: traced.append(kwargs))
    provenance = {"require_review": True}
    context = retriever.build_rag_context(None, "grammar", provenance=provenance, trace_id="t")
    assert "c1" in context and "Grammar" in context
    assert provenance["status"] == "hit" and provenance["verification"] == "unverified"
    assert provenance["require_review"]
    assert traced[0]["stage"] == "rag" and traced[0]["event"] == "hit"


@pytest.mark.parametrize(
    "mode,broken,expected",
    [
        ("optional", False, "no_match"),
        ("optional", True, "degraded"),
        ("required", False, "no_match"),
        ("required", True, "degraded"),
    ],
)
def test_optional_rag_records_degradation_required_rag_fails_closed(
    monkeypatch, mode, broken, expected
):
    def retrieve(*args, **kwargs):
        if broken:
            raise RuntimeError("secret-provider-message")
        return []

    monkeypatch.setattr(retriever, "retrieve", retrieve)
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kwargs: None)
    info = {}
    if mode == "required":
        with pytest.raises(RagContextRequiredError):
            retriever.build_rag_context(None, "x", mode=mode, provenance=info, trace_id="t")
    else:
        assert (
            retriever.build_rag_context(None, "x", mode=mode, provenance=info, trace_id="t") == ""
        )
    assert info["status"] == expected and "secret-provider-message" not in str(info)


def test_low_similarity_sources_are_not_claimed_as_hit(monkeypatch):
    def retrieve(*args, **kwargs):
        kwargs["details"].append({"chunk_id": "c1", "source_name": "Irrelevant", "similarity": 0.1})
        return ["unrelated"]

    monkeypatch.setattr(retriever, "retrieve", retrieve)
    info = {}
    assert retriever.build_rag_context(None, "x", provenance=info) == ""
    assert info["status"] == "no_match" and info["citations"] == []


def test_off_mode_never_calls_embedding(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("should not retrieve")

    monkeypatch.setattr(retriever, "retrieve", unexpected)
    assert retriever.build_rag_context(None, "x", mode="off") == ""


def test_unified_upload_passes_tenant_and_partial_vectors_do_not_write(db, monkeypatch):
    user = SimpleNamespace(id="u", tenant_id="a", role="researcher")
    monkeypatch.setattr("app.rag.indexer.embed_texts", lambda *args, **kwargs: [[1.0] * 1024])
    result = KnowledgeService(db).upload_file("grammar.txt", b"grammar rule", "教材", None, user)
    assert result["chunks"] == 1
    assert db.query(KnowledgeChunk).one().tenant_id == "a"
    monkeypatch.setattr("app.rag.indexer.embed_texts", lambda *args, **kwargs: [])
    with pytest.raises(InvalidKnowledgeError):
        index_document(db, "教材", "broken", "grammar rule", tenant_id="a")
    assert db.query(KnowledgeChunk).count() == 1


@pytest.mark.parametrize("content", [b"", b"   "])
def test_empty_upload_returns_typed_validation_error(db, content):
    user = SimpleNamespace(id="u", tenant_id="a", role="researcher")
    with pytest.raises(InvalidKnowledgeError):
        KnowledgeService(db).upload_file("file.txt", content, "教材", None, user)


def test_source_required_content_cannot_pass_or_publish_without_human_verification(db):
    state = seed(db)
    state.update(
        qc_score=80,
        dimension_scores={"a": 80},
        rag_provenance={
            "status": "hit",
            "require_review": True,
            "citations": [{"chunk_id": "c1", "source_name": "Grammar"}],
            "verification": "unverified",
        },
    )
    wf.store_node(state, db)
    item = db.query(ContentItem).one()
    user = SimpleNamespace(id="human", tenant_id=None, role="researcher")
    with pytest.raises(ContentStateConflictError):
        QualityService(db).review_content(item.id, QualityReviewRequest(**{"pass": True}), user)
    QualityService(db).review_content(
        item.id, QualityReviewRequest(**{"pass": True, "reference_verified": True}), user
    )
    assert item.provenance["reference_review"]["reviewer"] == "human"
    assert ContentService(db).publish_content(item.id, user).status == "published"


def test_required_rag_rejects_empty_query_before_generation(db, monkeypatch):
    from app.models import QuestionTemplate

    state = seed(db)
    template = db.query(QuestionTemplate).one()
    template.run_config = {"rag": {"mode": "required"}}
    db.commit()
    called = []
    monkeypatch.setattr(wf, "generate_with_fallback", lambda *args, **kwargs: called.append(True))
    monkeypatch.setattr(retriever, "retrieve", lambda *args, **kwargs: [])
    with pytest.raises(RagContextRequiredError):
        wf.generate_node(state, db)
    assert called == []


def test_embedding_dimension_mismatch_is_rejected_before_cloud_call(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EMBEDDING_DIM", 512)

    def forbidden(*args, **kwargs):
        raise AssertionError("should not call provider")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    with pytest.raises(InvalidKnowledgeError, match="Schema"):
        index_document(db, "教材", "bad", "rule", tenant_id="a")


def test_partial_embedding_response_records_failed_cost_instead_of_silently_losing_call(
    monkeypatch,
):
    from app.rag import embedding

    captured = []
    usage = SimpleNamespace(prompt_tokens=10)
    client = SimpleNamespace(
        embeddings=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(data=[], usage=usage))
    )
    monkeypatch.setattr(embedding, "_get_openai_client", lambda: client)
    monkeypatch.setattr(embedding, "record_trace", lambda **kwargs: captured.append(kwargs))
    with pytest.raises(InvalidKnowledgeError):
        embedding.embed_texts(["query"])
    assert len(captured) == 1 and captured[0]["success"] is False
    assert captured[0]["cost"] > 0 and captured[0]["usage_reported"] is True


def test_gold_report_separates_rag_hits_degradation_and_unrecorded_history():
    from copy import deepcopy

    from test_p0_gold_eval import case_data

    from app.engine.gold_eval import GoldCase, gold_report

    cases = []
    for index, status in enumerate(["hit", "degraded", None]):
        data = deepcopy(case_data())
        data["case_id"] = f"case-{index}"
        data["rag_provenance"] = {"status": status} if status is not None else {}
        cases.append(GoldCase.model_validate(data))
    result = gold_report(cases)
    assert set(result["by_rag_status"]) == {"hit", "degraded", "unknown"}
    assert all(group["n"] == 1 for group in result["by_rag_status"].values())
