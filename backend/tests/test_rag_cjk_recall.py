"""Bounded CJK recall and evidence-oriented eval. Providers are mocked here."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.models import KnowledgeChunk
from app.rag import retriever
from app.rag.knowledge_points import load_catalog
from app.rag.retrieval import keyword
from app.versioning import hash_value
from scripts.rag_eval import Case, corpus_manifest, score


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "bigram")
    monkeypatch.setattr(settings, "RAG_KEYWORD_MAX_TERMS", 32)
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RETRIEVAL_METHOD", "hybrid")
    monkeypatch.setattr(settings, "RAG_NEIGHBOR_WINDOW", 0)
    monkeypatch.setattr(settings, "RAG_MIN_SIMILARITY", 0.3)
    monkeypatch.setattr(
        retriever, "embed_texts", lambda inputs, **kwargs: [[1.0] + [0.0] * 1023 for _ in inputs]
    )


def row(db, content, vector=None, tenant="own", **kwargs):
    value = KnowledgeChunk(
        content=content,
        source_name="Test source",
        source_type="教材",
        tenant_id=tenant,
        chunk_type="single",
        embedding=vector,
        **kwargs,
    )
    db.add(value)
    db.commit()
    return value


def cite(text, **kwargs):
    return {
        "document_id": "doc",
        "page_no": 1,
        "chunk_id": "leaf",
        "table_id": "table",
        "content": text,
        "content_hash": hash_value(text),
        **kwargs,
    }


def table_case():
    return Case(
        id="rows",
        query="rule",
        relevant=[
            {
                "document_id": "doc",
                "pages": [1],
                "chunk_ids": ["leaf"],
                "table_id": "table",
                "required_terms": ["alpha", "beta"],
                "required_rows": [["alpha", "red"], ["beta", "blue"]],
            }
        ],
    )


def test_terms_preserve_original_and_english_symbols_with_cjk_pairs():
    values = keyword.terms("连系动词为什么需要表语？ -ed Unit-12")
    assert {"连系", "动词", "表语", "-ed", "unit-12"} <= set(values)
    assert len(values) <= 32 and len(set(values)) == len(values)
    assert keyword.terms("  ") == []


def test_legacy_mode_preserves_old_whole_run_and_16_cap(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "legacy")
    assert keyword.terms("连系动词为什么需要表语？") == [
        "连系动词为什么需要表语?",
        "连系动词为什么需要表语",
    ]
    assert len(keyword.terms(" ".join(f"word{i}" for i in range(50)))) == 16


def test_long_cjk_query_samples_both_ends_without_single_character_tokens(monkeypatch):
    monkeypatch.setattr(settings, "RAG_KEYWORD_MAX_TERMS", 4)
    query = "".join(chr(0x4E00 + i) for i in range(500))
    values = keyword.terms(query + "？")
    assert len(values) == 4 and query[:2] in values and query[-2:] in values
    assert all(len(value) >= 2 for value in values)


@pytest.mark.parametrize("maximum", [1, 65, -1])
def test_invalid_lexical_term_limit_is_rejected(maximum):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, RAG_KEYWORD_MAX_TERMS=maximum)


def test_unknown_keyword_mode_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, RAG_CJK_KEYWORD_MODE="domain-specific-routing")


def test_unscoped_hybrid_cjk_recovers_fourth_vector_candidate_without_scope_change(db, monkeypatch):
    for content, similarity in [("因果关系", 1), ("并列连词", 0.98), ("表示选择关系", 0.94)]:
        row(db, content, [similarity, (1 - similarity**2) ** 0.5] + [0.0] * 1022)
    correct = row(db, "| 连系动词 | 必须与表语构成系表结构 |", [0.8, 0.6] + [0.0] * 1022)
    query = "连系动词为什么需要表语？"
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "legacy")
    before = []
    retriever.retrieve(db, query, tenant_id="own", top_k=3, details=before)
    assert correct.id not in [c["chunk_id"] for c in before]
    monkeypatch.setattr(settings, "RAG_CJK_KEYWORD_MODE", "bigram")
    after, diagnostic = [], {}
    retriever.retrieve(db, query, tenant_id="own", top_k=3, details=after, diagnostics=diagnostic)
    assert after[0]["chunk_id"] == correct.id
    assert diagnostic["scope"]["canonical_name"] is None
    assert diagnostic["lanes"]["vector:0"]["chunk_ids"].index(correct.id) == 3
    assert diagnostic["lanes"]["keyword:0"]["chunk_ids"][0] == correct.id
    assert "表语" in diagnostic["lanes"]["keyword:0"]["terms"]
    assert all(
        "content" not in c for lane in diagnostic["lanes"].values() for c in lane["candidates"]
    )
    assert not diagnostic["fallbacks"]


def test_bigrams_do_not_expand_tenant_scope_or_match_sql_wildcards(db):
    own = row(db, "连系动词和表语的关系")
    row(db, "连系动词和表语的关系", tenant="foreign")
    scope = load_catalog().scope(None, "exact")
    result = keyword.recall(db, "连系动词为什么需要表语？", scope, "own", False, 30)
    assert [c.row.id for c in result] == [own.id]
    assert keyword.recall(db, "%_", scope, "own", False, 30) == []
    assert keyword.recall(db, "连系动词", scope, "own", False, 30, document_ids=[]) == []


def test_table_row_eval_rejects_swapped_relations_even_if_page_and_terms_match():
    case = table_case()
    result = score(case, [cite("| alpha | blue |\n| beta | red |")])
    assert result["page_hit"] and result["unit_hit"] and result["terms_in_supporting_context"]
    assert result["same_row_support"] is False and result["row_tuple_recall"] == 0
    correct = score(case, [cite("| alpha | red |\n| beta | blue |")])
    assert correct["same_row_support"] and correct["row_tuple_recall"] == 1


def test_correct_page_wrong_table_is_not_precise_support():
    result = score(table_case(), [cite("| alpha | red |\n| beta | blue |", table_id="other")])
    assert result["page_hit"] and not result["unit_hit"]
    assert not result["same_row_support"] and not result["terms_in_supporting_context"]


def test_parent_can_support_precise_child_identity_without_faking_table_id():
    case = Case(
        id="parent",
        query="rule",
        relevant=[{"document_id": "doc", "pages": [1], "chunk_ids": ["leaf"]}],
    )
    result = score(
        case, [cite("Rule", chunk_id="parent", matched_child_ids=["leaf"], table_id=None)]
    )
    assert result["unit_hit"] and result["same_row_support"] is None


def test_candidate_present_but_context_missing_has_honest_failure_stage():
    case = table_case()
    correct = cite("", content_hash=None)
    diagnostic = {
        "diagnostic_version": "rag-retrieval-v2",
        "lanes": {"vector:0": {"candidates": [correct]}, "keyword:0": {"candidates": []}},
        "fusion": [correct],
        "rerank": [correct],
    }
    result = score(case, [cite("wrong", table_id="other")], diagnostic)
    assert result["stage_hits"]["vector"] and result["failure_stage"] == "context_or_top_k"
    assert not result["stage_hits"]["keyword"]
    assert "stage_hits" not in score(
        case, [], {"lanes": {}}
    )  # v1 evidence is unavailable, not false.


@pytest.mark.parametrize("anchors", [[], [" "], ["x" * 129]])
def test_empty_or_unbounded_row_anchors_are_rejected(anchors):
    with pytest.raises(ValidationError):
        Case(
            id="invalid",
            query="q",
            relevant=[{"document_id": "d", "pages": [1], "required_rows": [anchors]}],
        )


def test_unknown_gold_annotation_cannot_silently_drop_constraint():
    with pytest.raises(ValidationError):
        Case(
            id="invalid",
            query="q",
            relevant=[{"document_id": "d", "pages": [1], "required_rowz": [["x"]]}],
        )


def test_corpus_fingerprint_includes_vectors_and_excludes_other_tenants(db):
    own = row(db, "Own source", [1.0] + [0.0] * 1023)
    initial = corpus_manifest(db, "own")
    row(db, "Foreign source", [0.0, 1.0] + [0.0] * 1022, tenant="foreign")
    assert corpus_manifest(db, "own") == initial
    own.embedding = [0.0, 1.0] + [0.0] * 1022
    db.commit()
    assert corpus_manifest(db, "own")["sha256"] != initial["sha256"]


def test_disagreeing_sources_remain_separate_unverified_citations(db):
    a = row(db, "教材甲：练习G1的答案为A。", [1.0] + [0.0] * 1023)
    b = row(db, "教材乙：练习G1的答案为B。", [1.0] + [0.0] * 1023)
    citations = []
    retriever.retrieve(db, "练习G1答案", tenant_id="own", top_k=2, details=citations)
    assert {c["chunk_id"] for c in citations} == {a.id, b.id}
    assert all(c["verification"] == "unverified" for c in citations)
    assert any("答案为A" in c["content"] for c in citations)
    assert any("答案为B" in c["content"] for c in citations)


def test_eval_cli_fails_closed_when_corpus_changes(db, tmp_path, monkeypatch):
    import json
    import sys
    from contextlib import nullcontext

    from scripts import rag_eval

    gold, output = tmp_path / "gold.json", tmp_path / "result.json"
    gold.write_text(json.dumps([table_case().model_dump()]), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag_eval", "--gold", str(gold), "--output", str(output), "--run-id", "fixture-drift"],
    )
    monkeypatch.setattr(rag_eval, "SessionLocal", lambda: nullcontext(db))
    snapshots = iter([{"sha256": "before"}, {"sha256": "after"}])
    monkeypatch.setattr(rag_eval, "corpus_manifest", lambda *args: next(snapshots))
    calls = []

    def mocked_retrieve(session, query, **kwargs):
        calls.append(kwargs["trace_id"])
        kwargs["details"].append(cite("| alpha | red |\n| beta | blue |"))

    monkeypatch.setattr(rag_eval, "retrieve", mocked_retrieve)
    assert rag_eval.main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert not report["corpus_unchanged"] and report["row_metrics"]["supported_cases"] == 1
    assert calls == ["rag-eval:fixture-drift:rows"]
    assert "API_KEY" not in json.dumps(report["runtime"])


@pytest.mark.parametrize("run_id", ["bad/run", "x" * 33])
def test_eval_cli_validates_run_identity_before_retrieval(tmp_path, monkeypatch, run_id):
    import sys

    from scripts import rag_eval

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rag_eval",
            "--gold",
            str(tmp_path / "missing.json"),
            "--output",
            str(tmp_path / "result.json"),
            "--run-id",
            run_id,
        ],
    )
    with pytest.raises(SystemExit) as error:
        rag_eval.main()
    assert error.value.code == 2


def test_cross_page_relevance_cannot_be_credited_from_repeated_first_page():
    case = Case(
        id="cross-page",
        query="rule",
        relevant=[
            {"document_id": "doc", "pages": [1], "chunk_ids": ["leaf"], "required_terms": ["选择"]},
            {
                "document_id": "doc",
                "pages": [2],
                "chunk_ids": ["continuation"],
                "required_terms": ["否则"],
            },
        ],
    )
    first = cite("选择规则", table_id=None)
    duplicate = cite("否则也出现于本页", chunk_id="other", table_id=None)
    wrong = score(case, [first, duplicate])
    assert wrong["precise_unit_recall"] == 0.5 and not wrong["terms_in_supporting_context"]
    assert not wrong["context_complete"]
    second = cite("否则的跨页例句", page_no=2, chunk_id="continuation", table_id=None)
    diagnostic = {
        "diagnostic_version": "rag-retrieval-v2",
        "lanes": {"vector:0": {"candidates": [first, second]}},
        "fusion": [first, second],
        "rerank": [first, second],
    }
    partial = score(case, [first, duplicate], diagnostic)
    assert partial["stage_unit_recall"]["vector"] == 1
    assert partial["failure_stage"] == "context_or_top_k"
    assert partial["failure_details"] == [{"unit": 1, "stage": "context_or_top_k"}]
    second = cite("否则的跨页例句", page_no=2, chunk_id="continuation", table_id=None)
    correct = score(case, [first, second])
    assert correct["precise_unit_recall"] == 1 and correct["terms_in_supporting_context"]
