"""STR-1/2 invariants. No OCR/embedding/chat provider calls in this suite."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.errors import InvalidKnowledgeError, TenantScopeDeniedError
from app.models import KnowledgeChunk, KnowledgeDocument
from app.rag import retriever
from app.rag.document import finalize_document, text_hash
from app.rag.indexer import index_document
from app.rag.knowledge_points import load_catalog
from app.rag.parser import _block, _finish, parse_text
from app.rag.preview import preview_document
from app.rag.retrieval.models import Candidate
from app.rag.retrieval.relation import expand_relations
from app.rag.structure import build_structure
from app.services.knowledge_service import KnowledgeService, StructureReview
from app.versioning import hash_value
from scripts.rag_eval import Case, score


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # This suite fixes the pre-STR5 leaf layout; new defaults have separate contracts.
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "legacy")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "legacy")
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 8192)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", None)
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda inputs, **kwargs: [[1.0] + [0.0] * 1023 for _ in inputs],
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda inputs, **kwargs: [[1.0] + [0.0] * 1023 for _ in inputs]
    )
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kwargs: None)


def source():
    document = _finish(
        "Chapter.pdf",
        [
            _block("heading", "第二章 并列句", level=1, page=1),
            _block("heading", "第二节 并列连词", level=2, page=2),
            _block("heading", "(二)表选择、条件关系", level=2, page=2),
            _block("paragraph", "or表示选择或否则。可以连接两种选择。", page=2),
            _block(
                "heading",
                "第二章|并列句",
                level=1,
                page=3,
                meta={"native_type": "header", "chapter_header_candidate": True},
            ),
            _block("paragraph", "Hurry up, or you will be late. 快点，否则会迟到。", page=3),
            _block("heading", "(三)表因果关系", level=2, page=3),
            _block("paragraph", "for表示补充说明的原因。", page=3),
        ],
        [],
        stats={"selected_pages": [1, 2, 3]},
    )
    document["source_hash"] = text_hash(document["text"])
    return document


def actor(tenant="owner", role="researcher"):
    return SimpleNamespace(id=None, tenant_id=tenant, role=role, status="active")


def persist(db, document=None, tenant="owner"):
    document = document or source()
    index_document(db, "教材", "Fixture", document, tenant_id=tenant)
    doc = (
        db.query(KnowledgeDocument)
        .filter_by(tenant_id=tenant)
        .order_by(KnowledgeDocument.created_at.desc())
        .all()[-1]
    )
    rows = (
        db.query(KnowledgeChunk)
        .filter_by(document_id=doc.id)
        .order_by(KnowledgeChunk.chunk_index)
        .all()
    )
    return doc, rows


def expanded(db, row, scope=None, top_k=1, pool=None):
    info = {}
    snippets, citations = expand_relations(
        db,
        pool or [Candidate(row, similarity=0.9)],
        scope or load_catalog().scope(None, "exact"),
        "owner",
        False,
        top_k,
        info,
    )
    return snippets, citations, info


def test_structure_is_pure_and_repairs_running_header_without_changing_source():
    document = source()
    original = copy.deepcopy(document)
    plan = build_structure(document)
    assert document == original
    blocks = {b["block_id"]: b for b in plan["blocks"]}
    assert blocks["block:4"]["role"] == "furniture"
    assert blocks["block:3"]["section_id"] == blocks["block:5"]["section_id"]
    assert blocks["block:5"]["section_path"][-1] == "(二)表选择、条件关系"
    assert blocks["block:3"]["unit_id"] == blocks["block:5"]["unit_id"]
    assert any(
        e["from"] == "block:3" and e["to"] == "block:5" and e["state"] == "accepted"
        for e in plan["edges"]
    )
    assert blocks["block:5"]["unit_id"] != blocks["block:7"]["unit_id"]
    assert build_structure(document)["signature"] == plan["signature"]


def test_real_chapter_heading_is_not_running_header():
    document = source()
    document["blocks"][4]["meta"] = {}
    document["blocks"][4]["text"] = "第三章 从句"
    finalize_document(document)
    plan = build_structure(document)
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in plan["edges"])
    assert any(
        b["reason"] == "new_chapter" and b["block_id"] == "block:4" for b in plan["barriers"]
    )


def test_missing_pages_reset_section_and_prevent_auto_join():
    document = source()
    for block in document["blocks"][4:]:
        block["page_no"] = 16
    plan = build_structure(document)
    assert any(b["reason"] == "non_contiguous_pages" for b in plan["barriers"])
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in plan["edges"])


def test_excluded_source_order_is_a_hard_barrier():
    document = source()
    document["blocks"].pop(4)
    finalize_document(document)
    plan = build_structure(document)
    assert any(b["reason"] == "excluded_or_missing_block" for b in plan["barriers"])
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in plan["edges"])


@pytest.mark.parametrize("role_text", ["链接中考", "【答案】B", "Question 2"])
def test_independent_question_or_answer_is_not_auto_joined(role_text):
    document = source()
    document["blocks"][4] = _block("heading", role_text, level=2, page=3)
    document["blocks"][4].update(block_id="block:4", order=4)
    finalize_document(document)
    plan = build_structure(document)
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in plan["edges"])
    assert any(b["reason"] == "independent_question_or_answer" for b in plan["barriers"])


def test_unknown_adjacency_is_proposed_and_reviewable_not_auto_accepted():
    document = parse_text("notes.md", "First unrelated paragraph.\n\nSecond independent paragraph.")
    plan = build_structure(document)
    edge = plan["edges"][0]
    assert edge["state"] == "proposed"
    approved = build_structure(document, accepted_edge_ids=[edge["id"]])
    assert (
        approved["edges"][0]["state"] == "accepted"
        and "reviewer_confirmed" in approved["edges"][0]["evidence"]
    )
    assert approved["signature"] != plan["signature"]
    rejected = build_structure(document, rejected_edge_ids=[edge["id"]])
    assert rejected["edges"][0]["state"] == "rejected"


@pytest.mark.parametrize(
    "damage", ["duplicate", "offset", "order", "too_many", "unknown_edge", "conflict"]
)
def test_malformed_structure_or_unknown_override_fails_before_models(damage):
    document = source()
    kwargs = {}
    if damage == "duplicate":
        document["blocks"][1]["block_id"] = "block:0"
    if damage == "offset":
        document["blocks"][1]["meta"]["content_end"] += 1
    if damage == "order":
        document["blocks"][1]["order"] = 0
    if damage == "too_many":
        kwargs["max_blocks"] = 1
    if damage == "unknown_edge":
        kwargs["accepted_edge_ids"] = ["unknown"]
    if damage == "conflict":
        kwargs.update(accepted_edge_ids=["x"], rejected_edge_ids=["x"])
    with pytest.raises(ValueError):
        build_structure(document, **kwargs)


def test_table_reference_and_native_caption_are_relations_not_table_merges():
    document = parse_text(
        "rules.md",
        "# Rule\n\nThe types are in the following table:\n\n"
        "| Type | Rule |\n| --- | --- |\n| A | red |\n| B | blue |",
    )
    plan = build_structure(document)
    assert any(
        e["relation"] == "definition_to_table" and e["state"] == "accepted" for e in plan["edges"]
    )
    table = next(b for b in plan["blocks"] if b["role"] == "table")
    assert table["table_header_recognized"] and len(table["table_line_spans"]) == 4


def test_preview_contains_structure_and_never_changes_original_document(monkeypatch):
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda *a, **k: pytest.fail("preview must not pay")
    )
    result = preview_document(source())
    assert result["structure"]["counts"]["furniture"] == 1 and result["parents"] == []


def test_relation_returns_cross_page_actual_segments_not_false_single_span(db):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    text, citations, info = expanded(db, seed)
    assert "Hurry up" in text[0] and "for表示" not in text[0]
    assert "第二章|并列句" not in text[0]
    citation = citations[0]
    assert citation["pages"] == [2, 3] and citation["page_no"] is None
    assert citation["content_start"] is None and citation["content_end"] is None
    assert info["bundle_count"] == 1 and info["segment_count"] > 1
    for segment in citation["source_segments"]:
        assert (
            segment["content"]
            == doc.normalized_text[segment["content_start"] : segment["content_end"]]
        )
        assert segment["content_hash"] == hash_value(segment["content"])
    assert not any(s["block_id"] == "block:4" for s in citation["source_segments"])


def test_multiple_seed_same_unit_dedup_and_separate_chapter_units(db):
    _, rows = persist(db)
    first = next(r for r in rows if "or表示" in r.content)
    continuation = next(r for r in rows if "Hurry up" in r.content)
    other = next(r for r in rows if "for表示" in r.content)
    _, citations, info = expanded(
        db, first, top_k=3, pool=[Candidate(first), Candidate(continuation), Candidate(other)]
    )
    assert len(citations) == 2 and info["bundle_count"] == 2
    assert citations[0]["pages"] == [2, 3]
    assert "for表示" in citations[1]["content"]


def test_scope_cannot_be_bypassed_by_cross_page_expansion(db):
    _, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    scope = load_catalog().scope(None, "exact")
    scope.section_path = list(seed.section_path)
    snippets, citations, _ = expanded(db, seed, scope)
    assert snippets and "Hurry up" not in snippets[0]
    assert "scope_or_active_member_missing" in citations[0]["incomplete_reasons"]


def test_stale_index_does_not_read_deleted_content_from_document(db):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    continuation = next(r for r in rows if "Hurry up" in r.content)
    KnowledgeService(db).delete_knowledge(continuation.id, actor())
    snippets, citations, _ = expanded(db, seed)
    assert "Hurry up" not in snippets[0] and doc.status == "partial_index"
    assert "stale_index_no_expansion" in citations[0]["incomplete_reasons"]


def test_foreign_leaf_is_not_in_active_members_or_materialized(db):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    continuation = next(r for r in rows if "Hurry up" in r.content)
    continuation.tenant_id = "foreign"
    db.commit()
    snippets, _, _ = expanded(db, seed)
    assert "Hurry up" not in snippets[0]
    assert continuation.id not in json.dumps(expanded(db, seed)[1])


def test_source_snapshot_mismatch_cannot_fabricate_body_from_document(db):
    _, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    seed.content = "different body"
    db.commit()
    snippets, citations, _ = expanded(db, seed)
    assert not snippets and not citations


def test_structure_review_is_free_revision_bound_and_changes_query(db, monkeypatch):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    service = KnowledgeService(db)
    plan = service.structure_preview(doc.id, actor())
    edge = next(e for e in plan["edges"] if e["from"] == "block:3" and e["to"] == "block:5")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda *a, **k: pytest.fail("free review cannot embed")
    )
    response = service.review_structure(
        doc.id,
        StructureReview(
            review_signature=plan["review_signature"],
            expected_index_revision=1,
            source_reviewed=True,
            rejected_edge_ids=[edge["id"]],
        ),
        actor(),
    )
    assert response["embedding_calls"] == 0 and not response["index_rebuilt"]
    assert response["structure_revision"] == 1
    snippets, _, _ = expanded(db, seed)
    assert "Hurry up" not in snippets[0]
    with pytest.raises(InvalidKnowledgeError):
        service.review_structure(
            doc.id,
            StructureReview(
                review_signature=plan["review_signature"],
                expected_index_revision=1,
                source_reviewed=True,
            ),
            actor(),
        )


@pytest.mark.parametrize("change", ["unreviewed", "revision", "unknown_edge"])
def test_structure_review_rejects_invalid_gate_or_decisions(db, change):
    doc, _ = persist(db)
    service = KnowledgeService(db)
    p = service.structure_preview(doc.id, actor())
    kwargs = {
        "review_signature": p["review_signature"],
        "expected_index_revision": 1,
        "source_reviewed": True,
    }
    if change == "unreviewed":
        kwargs["source_reviewed"] = False
    if change == "revision":
        kwargs["expected_index_revision"] = 2
    if change == "unknown_edge":
        kwargs["accepted_edge_ids"] = ["unknown"]
    with pytest.raises(InvalidKnowledgeError):
        service.review_structure(doc.id, StructureReview(**kwargs), actor())


def test_structure_preview_and_review_tenant_denied(db):
    doc, _ = persist(db)
    with pytest.raises(TenantScopeDeniedError):
        KnowledgeService(db).structure_preview(doc.id, actor("foreign"))


@pytest.mark.parametrize(
    "setting,value",
    [
        ("RAG_CONTEXT_BUNDLE_MAX_MEMBERS", 1),
        ("RAG_CONTEXT_BUNDLE_MAX_HOPS", 1),
        ("RAG_CONTEXT_MAX_SEGMENTS", 1),
    ],
)
def test_expansion_limits_are_real_and_report_incomplete(db, monkeypatch, setting, value):
    document = source()
    if setting == "RAG_CONTEXT_BUNDLE_MAX_HOPS":
        additions = [
            _block("paragraph", "Additional continuing example " + str(i), page=3) for i in range(4)
        ]
        document = _finish(
            "Chapter.pdf", [*document["blocks"][:6], *additions, *document["blocks"][6:]], []
        )
    _, rows = persist(db, document)
    seed = next(r for r in rows if "or表示" in r.content)
    monkeypatch.setattr(settings, setting, value)
    text, citations, info = expanded(db, seed)
    if citations:
        assert not citations[0]["complete"]
    if setting == "RAG_CONTEXT_MAX_SEGMENTS":
        assert info["segment_count"] <= value


@pytest.mark.parametrize("budget,bytes_budget", [(256, None), (512, 256), (64, 64)])
def test_total_context_budget_includes_labels_and_utf8(db, monkeypatch, budget, bytes_budget):
    _, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", budget)
    monkeypatch.setattr(settings, "RAG_CONTEXT_TOKEN_LIMIT", bytes_budget)
    text, _, info = expanded(db, seed)
    assert len("\n\n".join(text)) <= budget
    if bytes_budget:
        assert len("\n\n".join(text).encode("utf-8")) <= bytes_budget
    assert info["context_chars"] <= budget


def test_table_context_keeps_header_and_whole_rows_with_segment_mapping(db):
    doc, rows = persist(
        db,
        parse_text(
            "table.md",
            "# Rule\n\nThe following table:\n\n"
            "| Type | Rule |\n| --- | --- |\n| A | red |\n| B | blue |",
        ),
    )
    seed = next(r for r in rows if "| A |" in r.content)
    text, citations, _ = expanded(db, seed)
    assert "| Type | Rule |\n| --- | --- |\n| A | red |\n| B | blue |" in text[0]
    assert len([s for s in citations[0]["source_segments"] if s["table_id"]]) == 4
    assert all(
        s["content"] == doc.normalized_text[s["content_start"] : s["content_end"]]
        for s in citations[0]["source_segments"]
    )


def test_generation_provenance_contains_every_delivered_segment_not_only_seed(db):
    doc, _ = persist(db)
    provenance = {}
    text = retriever.build_rag_context(
        db, "or", tenant_id="owner", mode="required", context_mode="relation", provenance=provenance
    )
    assert text and provenance["protocol_version"] == "rag-context-v2"
    assert len(provenance["citations"]) >= len(provenance["bundle_citations"])
    assert all(c["verification"] == "unverified" for c in provenance["citations"])
    assert all(c["document_id"] == doc.id for c in provenance["citations"])


def test_v2_scoring_counts_actual_segments_beyond_top_k_but_not_claimed_children():
    case = Case(
        id="cross",
        query="q",
        top_k=1,
        relevant=[
            {"document_id": "doc", "pages": [2], "chunk_ids": ["a"], "required_terms": ["rule"]},
            {"document_id": "doc", "pages": [3], "chunk_ids": ["b"], "required_terms": ["example"]},
        ],
    )
    a = {
        "chunk_id": "a",
        "document_id": "doc",
        "page_no": 2,
        "content": "rule",
        "content_hash": hash_value("rule"),
    }
    b = {
        "chunk_id": "b",
        "document_id": "doc",
        "page_no": 3,
        "content": "example",
        "content_hash": hash_value("example"),
    }
    bundle = {
        "protocol_version": "rag-context-v2",
        "content": "rule example",
        "content_hash": hash_value("rule example"),
        "source_segments": [a, b],
        "matched_child_ids": ["a", "b", "never_delivered"],
    }
    result = score(case, [bundle])
    assert (
        result["context_complete"]
        and result["actual_segment_count"] == 2
        and result["returned_bundle_count"] == 1
    )
    bundle["source_segments"] = [a]
    result = score(case, [bundle])
    assert not result["context_complete"] and result["precise_unit_recall"] == 0.5


def test_v2_partial_source_and_wrong_hash_are_not_complete():
    case = Case(id="partial", query="q", relevant=[{"document_id": "doc", "pages": [1]}])
    source = {
        "chunk_id": "x",
        "document_id": "doc",
        "page_no": 1,
        "content": "raw",
        "content_hash": "incorrect",
        "truncated": True,
    }
    result = score(
        case,
        [
            {
                "protocol_version": "rag-context-v2",
                "content": "raw",
                "content_hash": hash_value("raw"),
                "source_segments": [source],
            }
        ],
    )
    assert (
        result["unit_hit"] and not result["context_complete"] and not result["snapshot_hash_valid"]
    )


@pytest.mark.parametrize("mode", ["legacy", "relation"])
def test_retrieve_protocol_preserves_zipped_fields_and_no_additional_provider_queries(
    db, monkeypatch, mode
):
    persist(db)
    calls = []
    monkeypatch.setattr(
        retriever,
        "embed_texts",
        lambda texts, **kwargs: calls.append(texts) or [[1.0] + [0.0] * 1023 for _ in texts],
    )
    details, info = [], {}
    snippets = retriever.retrieve(
        db, "or", tenant_id="owner", top_k=2, context_mode=mode, details=details, diagnostics=info
    )
    assert len(snippets) == len(details) <= 2 and len(calls) == 1
    assert info["protocol_version"] == (
        "rag-context-v2" if mode == "relation" else "rag-context-v1"
    )


@pytest.mark.parametrize(
    "field",
    [
        "RAG_STRUCTURE_MAX_BLOCKS",
        "RAG_STRUCTURE_MAX_LEAFS",
        "RAG_CONTEXT_BUNDLE_MAX_MEMBERS",
        "RAG_CONTEXT_BUNDLE_MAX_HOPS",
        "RAG_CONTEXT_MAX_SEGMENTS",
    ],
)
def test_invalid_limits_are_validated(field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_invalid_context_mode_fails_before_embedding(db, monkeypatch):
    monkeypatch.setattr(
        retriever, "embed_texts", lambda *a, **k: pytest.fail("invalid mode cannot pay")
    )
    with pytest.raises(InvalidKnowledgeError):
        retriever.retrieve(db, "q", context_mode="unknown")


def test_api_free_structure_permission_and_v2_schema(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router
    from app.database import get_db
    from app.security import get_current_user

    doc, _ = persist(db)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: actor(role="viewer")
    client = TestClient(application)
    assert client.get(f"/api/knowledge/documents/{doc.id}/structure").status_code == 403
    assert (
        client.post(f"/api/knowledge/documents/{doc.id}/structure-review", json={}).status_code
        == 403
    )
    application.dependency_overrides[get_current_user] = lambda: actor()
    response = client.get(f"/api/knowledge/documents/{doc.id}/structure")
    assert response.status_code == 200 and response.json()["counts"]["furniture"] == 1
    body = {
        "review_signature": response.json()["review_signature"],
        "expected_index_revision": 1,
        "source_reviewed": True,
    }
    result = client.get(
        "/api/knowledge/retrieve", params={"query": "or", "context_mode": "relation", "top_k": 1}
    )
    assert result.status_code == 200 and result.json()["protocol_version"] == "rag-context-v2"
    assert result.json()["bundles"] and result.json()["citations"][0]["source_segments"]
    assert (
        client.get(
            "/api/knowledge/retrieve", params={"query": "or", "context_mode": "invalid"}
        ).status_code
        == 422
    )
    assert (
        client.post(f"/api/knowledge/documents/{doc.id}/structure-review", json=body).status_code
        == 200
    )


def test_structure_disabled_and_legacy_source_fallback_do_not_expand(db, monkeypatch):
    _, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", False)
    text, cites, info = expanded(db, seed)
    assert (
        "Hurry up" not in text[0] and "legacy_source_no_structure" in cites[0]["incomplete_reasons"]
    )
    assert info["protocol_version"] == "rag-context-v2"


def test_policy_signature_drift_refuses_expansion(db, monkeypatch):
    _, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    from app.rag.structure import builder

    original = builder.policy
    monkeypatch.setattr(builder, "policy", lambda: (original()[0], "different-policy"))
    text, cites, _ = expanded(db, seed)
    assert (
        "Hurry up" not in text[0] and "structure_policy_changed" in cites[0]["incomplete_reasons"]
    )


def test_reviewer_source_signature_drift_is_not_silently_reapproved(db):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    service = KnowledgeService(db)
    plan = service.structure_preview(doc.id, actor())
    service.review_structure(
        doc.id,
        StructureReview(
            review_signature=plan["review_signature"],
            expected_index_revision=1,
            source_reviewed=True,
        ),
        actor(),
    )
    doc.meta = {**doc.meta, "index_revision": 2}
    db.commit()
    text, cites, _ = expanded(db, seed)
    assert "Hurry up" not in text[0] and "structure_review_stale" in cites[0]["incomplete_reasons"]


def test_free_review_of_stale_index_rejected_even_with_current_signature(db):
    doc, rows = persist(db)
    service = KnowledgeService(db)
    victim = next(r for r in rows if "Hurry up" in r.content)
    service.delete_knowledge(victim.id, actor())
    plan = service.structure_preview(doc.id, actor())
    assert not plan["expansion_allowed"]
    with pytest.raises(InvalidKnowledgeError):
        service.review_structure(
            doc.id,
            StructureReview(
                review_signature=plan["review_signature"],
                expected_index_revision=1,
                source_reviewed=True,
            ),
            actor(),
        )


def test_preview_and_index_bind_structure_decisions_before_any_fee(db, monkeypatch):
    document = source()
    plan = build_structure(document)
    edge = next(e for e in plan["edges"] if e["from"] == "block:3" and e["to"] == "block:5")
    calls = []
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: calls.append(values) or [[1.0] + [0.0] * 1023 for _ in values],
    )
    with pytest.raises(ValueError):
        index_document(
            db,
            "教材",
            "Fixture",
            document,
            tenant_id="owner",
            structure_decisions={"accepted_edge_ids": ["bad"]},
        )
    assert calls == []
    index_document(
        db,
        "教材",
        "Fixture",
        document,
        tenant_id="owner",
        structure_decisions={"rejected_edge_ids": [edge["id"]]},
    )
    doc = db.query(KnowledgeDocument).one()
    seed = next(r for r in db.query(KnowledgeChunk) if "or表示" in r.content)
    assert doc.meta["structure_decisions"]["rejected_edge_ids"] == [edge["id"]]
    assert "Hurry up" not in expanded(db, seed)[0][0]


def test_table_row_budget_never_breaks_protected_row_or_fence(db, monkeypatch):
    document = parse_text(
        "long.md",
        "# Rule\n\n| Key | Value |\n| --- | --- |\n| A | " + "long " * 20 + " |\n| B | short |",
    )
    _, rows = persist(db, document)
    seed = next(r for r in rows if "| A |" in r.content)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 256)
    _, cites, _ = expanded(db, seed)
    for cite in cites:
        for segment in cite["source_segments"]:
            if segment["table_id"]:
                assert not segment["truncated"] and segment["content"].rstrip().endswith("|")


def test_noncontiguous_segments_are_not_forged_as_one_source_span(db):
    doc, rows = persist(db)
    seed = next(r for r in rows if "or表示" in r.content)
    _, cites, _ = expanded(db, seed)
    assert cites[0]["content_start"] is None
    spans = [(s["content_start"], s["content_end"]) for s in cites[0]["source_segments"]]
    assert any(a[1] < b[0] for a, b in zip(spans, spans[1:]))
    assert all(doc.normalized_text[a:b] for a, b in spans)


def test_audit_snapshot_dedups_chunk_ids_but_preserves_every_segment_hash():
    from app.rag.provenance import delivered_citations, reference_snapshot

    entries = [
        {
            "protocol_version": "rag-context-v2",
            "source_segments": [
                {"chunk_id": "a", "segment_id": "one", "content_hash": "x"},
                {"chunk_id": "a", "segment_id": "two", "content_hash": "y"},
            ],
        }
    ]
    snap = reference_snapshot(delivered_citations(entries))
    assert snap["chunk_ids"] == ["a"] and snap["segment_ids"] == ["one", "two"]
    assert [s["content_hash"] for s in snap["source_snapshots"]] == ["x", "y"]


def test_generate_node_injects_v2_context_separately_and_keeps_all_sources(db, monkeypatch):
    from app.models import QuestionTemplate
    from app.workflow import graph
    from tests.test_p0_quality_events import seed

    state = seed(db)
    state["params"]["knowledge_point"] = "并列句"
    state["params"]["tenant_id"] = "owner"
    template = db.query(QuestionTemplate).one()
    template.run_config = {"rag": {"mode": "required", "context_mode": "relation"}}
    db.commit()
    index_document(db, "教材", "Fixture", source(), knowledge_point="并列句", tenant_id="owner")
    captured = {}

    def generate(template, params, *args, **kwargs):
        captured.update(params)
        return {"fixture": "mock draft"}

    monkeypatch.setattr(graph, "generate_with_fallback", generate)
    result = graph.generate_node(state, db)
    assert result["rag_provenance"]["protocol_version"] == "rag-context-v2"
    assert captured["rag_context"] and "rag_context" not in state["params"]
    assert all(
        c["segment_id"] and c["content_hash"] == hash_value(c["content"])
        for c in result["rag_provenance"]["citations"]
    )


def test_ocr_review_binds_edge_decisions_and_refuses_unknown_edges_before_fee(
    db, tmp_path, monkeypatch
):
    from app.services.ocr_review import ReviewApproval, approve, plan
    from tests.test_ocr_review import confirmation, ready, runtime

    rt = runtime.__wrapped__(db, tmp_path, monkeypatch)
    identifier = ready(db, rt)
    before = plan(db, identifier, rt[1])
    edge = before["structure"]["edges"][0]
    after = plan(db, identifier, rt[1], None, None, [edge["id"]])
    assert (
        after["plan_hash"] != before["plan_hash"]
        and after["preview_hash"] == before["preview_hash"]
    )
    with pytest.raises(InvalidKnowledgeError):
        approve(
            db,
            identifier,
            confirmation(db, identifier, rt[1], rejected_edge_ids=[edge["id"]]),
            rt[1],
        )
    with pytest.raises(InvalidKnowledgeError):
        plan(db, identifier, rt[1], None, ["unknown"], None)
    body = ReviewApproval(
        preview_hash=after["preview_hash"],
        plan_hash=after["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        rejected_edge_ids=[edge["id"]],
    )
    assert approve(db, identifier, body, rt[1])["index_status"] == "pending"


def test_rule_example_and_exception_across_paragraphs_form_one_context(db):
    document = parse_text(
        "rule.md",
        "# Rule\n\nA general grammar rule applies.\n\nExample: A concrete sentence.\n\n"
        "## 例外说明\n\nBut one exception needs special handling.\n\n"
        "# Other concept\n\nIndependent definition.",
    )
    _, rows = persist(db, document)
    seed = next(r for r in rows if "general grammar" in r.content)
    text, _, _ = expanded(db, seed)
    assert "concrete sentence" in text[0] and "special handling" in text[0]
    assert "Independent definition" not in text[0]


@pytest.mark.parametrize("title", ["第三章 新概念", "第3章 新概念"])
def test_different_chapter_header_is_a_barrier_even_without_body_title(title):
    document = source()
    document["blocks"][4]["text"] = title
    finalize_document(document)
    p = build_structure(document)
    assert any(b["reason"] == "header_indicates_different_chapter" for b in p["barriers"])
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in p["edges"])


def test_same_chapter_arabic_header_alias_keeps_subsection():
    document = source()
    document["blocks"][4]["text"] = "第2章 并列句"
    finalize_document(document)
    p = build_structure(document)
    assert any(e["from"] == "block:3" and e["to"] == "block:5" for e in p["edges"])


def test_untyped_chapter_marker_never_auto_joins_previous_knowledge():
    document = source()
    b = document["blocks"][4]
    b["text"] = "第三章 新概念"
    b["block_type"] = "paragraph"
    b["meta"] = {}
    finalize_document(document)
    p = build_structure(document)
    assert any(b["reason"] == "unverified_chapter_boundary" for b in p["barriers"])
    assert not any(e["from"] == "block:3" and e["to"] == "block:5" for e in p["edges"])


@pytest.mark.parametrize(
    "first,second", [("1.A 考查并列句。", "2.A考查并列句。"), ("4. A 考查连词。", "5.B考查连词。")]
)
def test_numbered_answer_explanations_are_independent_not_one_big_parent(first, second):
    document = _finish(
        "answers.pdf",
        [
            _block("heading", "参考答案", level=1, page=7),
            _block("paragraph", first, page=7),
            _block("paragraph", second, page=8),
        ],
        [],
    )
    p = build_structure(document)
    items = {b["block_id"]: b for b in p["blocks"]}
    assert items["block:1"]["role"] == "answer" and items["block:2"]["role"] == "answer"
    assert not any(e["from"] == "block:1" and e["to"] == "block:2" for e in p["edges"])
