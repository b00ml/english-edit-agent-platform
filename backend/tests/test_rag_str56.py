"""STR5 source-mapped splitting/rebuild and explicit STR6 experimental contracts."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.errors import ChunkingError, InvalidKnowledgeError
from app.models import KnowledgeChunk, KnowledgeDocument, OcrJob
from app.rag import retriever
from app.rag.chunking import split_document
from app.rag.chunking.models import ChunkingConfig
from app.rag.chunking.parents import parent_plans
from app.rag.chunking.structural import VERSION, split_structural, validate_structural
from app.rag.document import finalize_document
from app.rag.experiments.contextual import prefixes
from app.rag.experiments.late import LocalTokenEncoder, pool_tokens
from app.rag.experiments.semantic import cosine, semantic_chunks
from app.rag.indexer import index_document
from app.rag.parser import parse_text
from app.services.knowledge_service import KnowledgeService
from app.services.ocr_review import ReviewApproval, approve, plan
from app.versioning import hash_value
from app.worker.ocr_index import run_index
from tests.test_ocr_review import ready, runtime
from tests.test_rag_str12 import actor, source


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "legacy")
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda texts, **kwargs: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda texts, **kwargs: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kwargs: None)


def config(size=512):
    return ChunkingConfig(layout="structure", chunk_size=size, chunk_overlap=min(40, size // 2))


def test_structural_soft_pages_excludes_furniture_and_maps_noncontiguous_body():
    d = source()
    before = copy.deepcopy(d)
    chunks, diag = split_structural(d, config())
    combined = next(c for c in chunks if "or表示" in c.content)
    assert "Hurry up" in combined.content and "第二章|并列句" not in combined.content
    assert combined.page_no is None and combined.chunker_version == VERSION
    assert combined.as_meta()["pages"] == [2, 3]
    assert (
        combined.as_meta()["content_start"] is None
        and combined.as_meta()["source_envelope"]["not_a_contiguous_body_span"]
    )
    assert combined.source_segments and d == before
    assert (
        diag.layout == "structure"
        and diag.coverage_ratio == 1
        and "block:4" in diag.excluded_navigation_blocks
    )
    for s in combined.source_segments:
        assert (
            combined.content[s["content_start"] : s["content_end"]]
            == d["text"][s["source_start"] : s["source_end"]]
        )


def test_default_legacy_chunks_unchanged_and_structure_explicit():
    old, _ = split_document(source(), ChunkingConfig())
    new, _ = split_document(source(), config())
    assert all(c.chunker_version == "rag-chunker-v2" and not c.source_segments for c in old)
    assert len(new) < len(old)


@pytest.mark.parametrize("boundary", ["chapter", "gap", "excluded", "question", "answer"])
def test_structural_split_cannot_overlap_across_hard_boundary(boundary):
    d = source()
    if boundary == "chapter":
        d["blocks"][4]["text"] = "第三章 New"
        d["blocks"][4]["meta"] = {}
    if boundary == "gap":
        for b in d["blocks"][4:]:
            b["page_no"] = 16
    if boundary == "excluded":
        d["blocks"].pop(4)
    if boundary in {"question", "answer"}:
        b = d["blocks"][4]
        b["meta"] = {}
        b["text"] = "链接中考" if boundary == "question" else "【答案】B"
    finalize_document(d)
    chunks, _ = split_structural(d, config())
    assert not any("or表示" in c.content and "Hurry up" in c.content for c in chunks)


def test_structural_tables_keep_rows_headers_and_role_boundaries():
    d = parse_text(
        "table.md",
        "# Rule\n\nThe following table:\n\n| Key | Rule |\n| --- | --- |\n"
        + "".join(f"| K{i} | sentence {i}. |\n" for i in range(30)),
    )
    chunks, diag = split_structural(d, config(160))
    tables = [c for c in chunks if c.table_id]
    assert len(tables) > 1 and all("| Key | Rule |" in c.embedding_content for c in tables)
    assert not any("following" in c.content and c.table_id for c in chunks)
    assert all(len(c.embedding_content) <= 160 for c in chunks)
    assert diag.source_coverage_scope.startswith("eligible")


def test_structural_long_cell_preserves_partial_warning_not_full_row_proof():
    d = parse_text(
        "table.md",
        "# Rule\n\n| Key | Rule |\n| --- | --- |\n| A | " + ("very long cell " * 60) + " |",
    )
    chunks, _ = split_structural(d, config(180))
    assert any(c.hard_split and c.table_id for c in chunks)
    assert any("超长" in w for c in chunks for w in c.warning)


def test_structural_low_budget_fails_not_drop_context_or_source():
    d = source()
    with pytest.raises(ChunkingError):
        split_structural(d, config(8))


def test_structural_parent_has_own_exact_mapping_and_no_furniture(monkeypatch):
    d = source()
    monkeypatch.setattr(settings, "RAG_PARENT_MIN_CHARS", 1)
    chunks, _ = split_structural(d, config(105))
    parents = parent_plans(d, chunks)
    assert parents
    for p in parents:
        assert p.chunk.source_segments and "第二章|并列句" not in p.chunk.content
        assert all(
            p.chunk.content[s["content_start"] : s["content_end"]]
            == d["text"][s["source_start"] : s["source_end"]]
            for s in p.chunk.source_segments
        )
    assert not any("for表示" in p.chunk.content and "or表示" in p.chunk.content for p in parents)


def test_structural_ingest_relation_actual_segments_and_deleted_source_safety(db):
    d = source()
    index_document(db, "教材", "Fixture", d, tenant_id="owner", chunk_layout="structure")
    doc = db.query(KnowledgeDocument).one()
    rows = list(db.query(KnowledgeChunk).filter(KnowledgeChunk.chunk_type != "parent"))
    assert all(r.content_start is None and r.content_end is None for r in rows)
    assert all(r.chunker_version == VERSION for r in rows)
    details = []
    info = {}
    retriever.retrieve(db, "or表示", tenant_id="owner", details=details, diagnostics=info)
    assert not info["fallbacks"] and any(c["pages"] == [2, 3] for c in details)
    actual = [s for c in details for s in c["source_segments"]]
    assert all(
        s["content"] == doc.normalized_text[s["content_start"] : s["content_end"]] for s in actual
    )
    victim = next(r for r in rows if "Hurry up" in r.content)
    KnowledgeService(db).delete_knowledge(victim.id, actor())
    details = []
    retriever.retrieve(db, "or", tenant_id="owner", details=details)
    assert "Hurry up" not in " ".join(c["content"] for c in details)


def test_changed_source_segment_hash_cannot_use_snapshot_to_fabricate_content(db):
    index_document(db, "教材", "Fixture", source(), tenant_id="owner", chunk_layout="structure")
    row = next(r for r in db.query(KnowledgeChunk) if "Hurry up" in r.content)
    data = copy.deepcopy(row.meta)
    data["chunk"]["source_segments"][0]["content_hash"] = "0" * 64
    row.meta = data
    db.commit()
    details = []
    retriever.retrieve(db, "or", tenant_id="owner", details=details)
    assert row.id not in [s["chunk_id"] for c in details for s in c.get("source_segments", [])]


def test_layout_change_plan_hash_gates_fee_and_ocr_worker_atomic_revision(
    db, tmp_path, monkeypatch
):
    rt = runtime.__wrapped__(db, tmp_path, monkeypatch)
    jid = ready(db, rt)
    old = plan(db, jid, rt[1], chunk_layout="legacy")
    new = plan(db, jid, rt[1], chunk_layout="structure")
    assert old["preview_hash"] == new["preview_hash"] and old["plan_hash"] != new["plan_hash"]
    mismatch = ReviewApproval(
        preview_hash=old["preview_hash"],
        plan_hash=old["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        chunk_layout="structure",
    )
    with pytest.raises(InvalidKnowledgeError):
        approve(db, jid, mismatch, rt[1])
    body = ReviewApproval(
        preview_hash=new["preview_hash"],
        plan_hash=new["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        chunk_layout="structure",
    )
    approve(db, jid, body, rt[1])
    assert run_index(jid, rt[2])["status"] == "indexed"
    doc = db.query(KnowledgeDocument).one()
    assert doc.meta["chunk_layout"] == "structure"
    assert doc.meta["index_input_signature"] and db.get(OcrJob, jid).index_status == "indexed"


def test_structural_failed_rebuild_preserves_old_leafs_revision(db, monkeypatch):
    index_document(db, "教材", "Fixture", source(), tenant_id="owner", meta={"index_revision": 1})
    doc = db.query(KnowledgeDocument).one()
    ids = {r.id for r in db.query(KnowledgeChunk)}
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda values, **kw: [[float("nan")] * 1024 for _ in values]
    )
    with pytest.raises(InvalidKnowledgeError):
        index_document(
            db,
            "教材",
            "Fixture",
            source(),
            tenant_id="owner",
            document_id=doc.id,
            replace_existing=True,
            chunk_layout="structure",
            meta={"index_revision": 2},
        )
    assert {r.id for r in db.query(KnowledgeChunk)} == ids and doc.meta["index_revision"] == 1


def test_structural_validator_rejects_offset_or_content_damage():
    d = source()
    chunks, _ = split_structural(d, config())
    eligible = {s["block_id"] for c in chunks for s in c.source_segments}
    damaged = copy.deepcopy(chunks)
    damaged[0].source_segments[0]["source_end"] += 1
    with pytest.raises(ChunkingError):
        validate_structural(damaged, d, eligible, config())


def prose():
    return parse_text(
        "plain.md",
        "Alpha sentence. Another similar sentence. Completely different topic. A final example.",
    )


def test_semantic_explicit_similarity_boundary_preserves_all_source():
    d = prose()
    calls = []

    def embed(values):
        calls.append(values)
        return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]

    chunks, info = semantic_chunks(d, config(), embed, threshold=0.8)
    assert len(chunks) == 2 and len(calls) == 1 and info["sentence_inputs"] == 4
    assert any("different topic" in c.content for c in chunks)
    assert all(c.chunker_version == "experiment-semantic-v1" for c in chunks)


def test_semantic_never_calls_provider_for_table_code_or_explicit_units():
    d = parse_text(
        "protected.md",
        "# Topic\n\nA definition. An example.\n\n"
        "| Key | Value |\n| --- | --- |\n| A | B |\n\n```py\na=1\n```",
    )
    chunks, info = semantic_chunks(
        d, config(), lambda values: pytest.fail("protected source cannot be semantic split")
    )
    assert chunks and info["sentence_inputs"] == 0


def test_semantic_call_cap_validates_before_provider():
    with pytest.raises(ValueError):
        semantic_chunks(
            prose(), config(), lambda values: pytest.fail("cap before fee"), max_sentences=2
        )


@pytest.mark.parametrize(
    "vectors",
    [
        [[0.0, 0.0]] * 4,
        [[float("nan"), 1.0]] * 4,
        [[1.0, 0.0]],
        [[1.0, 0.0], [1.0], [1.0, 0.0], [1.0, 0.0]],
    ],
)
def test_semantic_invalid_vectors_fail_closed(vectors):
    with pytest.raises(ValueError):
        semantic_chunks(prose(), config(), lambda values: vectors)


def test_deterministic_context_is_derived_not_raw_and_budget_checked():
    d = prose()
    chunks, _ = split_structural(d, config())
    raw = copy.deepcopy(chunks)
    inputs, meta = prefixes(chunks, d, config())
    assert chunks == raw and all(
        m["raw_hash"] == hash_value(c.content) for c, m in zip(chunks, meta)
    )
    assert all(not m["model_derived"] for m in meta) and "plain" in inputs[0]
    with pytest.raises(ValueError):
        prefixes(chunks, d, config(16))


def test_context_llm_budget_is_checked_before_any_call():
    d = source()
    chunks, _ = split_structural(d, config())
    with pytest.raises(ValueError):
        prefixes(chunks, d, config(), llm=True, max_calls=0)


def test_context_llm_free_summary_is_rejected_and_traced(monkeypatch):
    from app.rag.experiments import contextual

    d = prose()
    chunks, _ = split_structural(d, config())
    traces = []
    response = SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"excerpts":["Invented information"]}')
            )
        ],
    )
    monkeypatch.setattr(
        contextual,
        "_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))
        ),
    )
    monkeypatch.setattr(contextual, "record_trace", lambda **kwargs: traces.append(kwargs))
    with pytest.raises(ValueError):
        prefixes(chunks, d, config(), llm=True, trace_id="fixture")
    assert traces[0]["stage"] == "rag_context_exp" and not traces[0]["success"]


def token_artifact():
    return {
        "kind": "contextual_token_hidden_states",
        "text_hash": hash_value("abcd"),
        "offsets": [[0, 0], [0, 1], [1, 2], [2, 3], [3, 4]],
        "attention_mask": [1] * 5,
        "states": [[999.0, 999.0], [1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]],
    }


def test_late_pool_uses_contextual_tokens_then_mean_normalizes_without_special_tokens():
    result = pool_tokens("abcd", token_artifact(), [[(0, 2)], [(2, 4)]])
    assert result == [[1.0, 0.0], [0.0, 1.0]]
    cross = pool_tokens("abcd", token_artifact(), [[(0, 1), (3, 4)]])[0]
    assert cross == pytest.approx([2**-0.5, 2**-0.5])


@pytest.mark.parametrize(
    "damage",
    [
        "hash",
        "final_vector",
        "truncated",
        "shape",
        "nan",
        "mask",
        "range",
        "nonmonotonic",
        "zero",
        "cap",
    ],
)
def test_late_pool_rejects_fake_final_vectors_truncation_or_invalid_offsets(damage):
    artifact = token_artifact()
    ranges = [[(0, 4)]]
    limit = 8192
    if damage == "hash":
        artifact["text_hash"] = "bad"
    if damage == "final_vector":
        artifact["kind"] = "final_text_embedding"
    if damage == "truncated":
        artifact["offsets"][-1] = [3, 3]
    if damage == "shape":
        artifact["states"][1] = [1.0]
    if damage == "nan":
        artifact["states"][1] = [float("nan"), 1.0]
    if damage == "mask":
        artifact["attention_mask"][1] = 2
    if damage == "range":
        ranges = [[(0, 5)]]
    if damage == "nonmonotonic":
        artifact["offsets"][2] = [0, 2]
    if damage == "zero":
        artifact["states"] = [[0.0, 0.0]] * 5
    if damage == "cap":
        limit = 2
    with pytest.raises(ValueError):
        pool_tokens("abcd", artifact, ranges, limit)


def test_late_missing_model_directory_does_not_download_or_use_cloud(tmp_path):
    with pytest.raises(ValueError):
        LocalTokenEncoder(tmp_path / "missing")


def test_experiment_cli_default_dry_run_never_calls_embedding_or_llm(tmp_path, monkeypatch):
    import sys

    from scripts import rag_experiment

    d = source()
    input = tmp_path / "source.json"
    output = tmp_path / "report.json"
    input.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["rag_experiment", "--input", str(input), "--output", str(output), "--variant", "semantic"],
    )
    monkeypatch.setattr(
        rag_experiment, "embed_texts", lambda *a, **k: pytest.fail("dry-run cannot pay")
    )
    assert rag_experiment.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert (
        report["status"] == "dry_run_prepared"
        and report["provider_batches"] == []
        and not report["production_index_changed"]
    )


@pytest.mark.parametrize(
    "arguments", [["--run"], ["--variant", "late", "--run"], ["--allow-llm-context"]]
)
def test_experiment_cli_refuses_implicit_fee_or_fake_late(arguments, tmp_path, monkeypatch):
    import sys

    from scripts import rag_experiment

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rag_experiment",
            "--input",
            str(tmp_path / "missing"),
            "--output",
            str(tmp_path / "out"),
            *arguments,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        rag_experiment.main()
    assert exc.value.code == 2


def test_structural_validator_rejects_unattributed_generated_text():
    d = source()
    chunks, _ = split_structural(d, config())
    eligible = {s["block_id"] for c in chunks for s in c.source_segments}
    damaged = copy.deepcopy(chunks)
    damaged[0].content += " invented facts"
    with pytest.raises(ChunkingError):
        validate_structural(damaged, d, eligible, config())


def test_structural_stale_composite_source_still_has_exact_delivered_coordinates(db):
    index_document(db, "教材", "Fixture", source(), tenant_id="owner", chunk_layout="structure")
    doc = db.query(KnowledgeDocument).one()
    doc.status = "partial_index"
    db.commit()
    citations = []
    retriever.retrieve(db, "or表示", tenant_id="owner", details=citations)
    actual = [s for c in citations for s in c.get("source_segments", [])]
    assert actual and all(
        s["content"] == doc.normalized_text[s["content_start"] : s["content_end"]] for s in actual
    )
    assert all(not c["complete"] for c in citations)


def test_source_block_gold_is_stable_across_leaf_id_rebuild():
    from scripts.rag_eval import Case, score

    case = Case(
        id="stable",
        query="q",
        relevant=[
            {
                "document_id": "doc",
                "pages": [2],
                "source_block_ids": ["block:1"],
                "required_terms": ["rule"],
            }
        ],
    )
    c = {
        "chunk_id": "new-id",
        "document_id": "doc",
        "page_no": 2,
        "block_id": "block:1",
        "content": "rule",
        "content_hash": hash_value("rule"),
    }
    assert score(case, [c])["context_complete"]
    c["block_id"] = "wrong"
    assert not score(case, [c])["unit_hit"]


def test_context_extract_success_preserves_raw_text_and_reports_derivation(monkeypatch):
    from app.rag.experiments import contextual

    d = prose()
    chunks, _ = split_structural(d, config())
    raw = [c.content for c in chunks]
    traces = []
    response = SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(message=SimpleNamespace(content='{"excerpts":["Alpha sentence."]}'))
        ],
    )
    monkeypatch.setattr(
        contextual,
        "_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))
        ),
    )
    monkeypatch.setattr(contextual, "record_trace", lambda **kwargs: traces.append(kwargs))
    values, meta = prefixes(chunks, d, config(), llm=True, trace_id="fixture")
    assert (
        [c.content for c in chunks] == raw and meta[0]["model_derived"] and "源文摘录" in values[0]
    )
    assert traces[0]["success"]


def test_experiment_cli_real_final_vector_run_keeps_corpus_unwritten(tmp_path, monkeypatch):
    import sys

    from scripts import rag_experiment

    doc = prose()
    inp = tmp_path / "source.json"
    gold = tmp_path / "gold.json"
    out = tmp_path / "out.json"
    inp.write_text(json.dumps(doc), encoding="utf-8")
    gold.write_text(
        json.dumps(
            [
                {
                    "id": "q",
                    "query": "Alpha",
                    "expected_block_ids": ["block:0"],
                    "required_terms": ["Alpha"],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "exp",
            "--input",
            str(inp),
            "--gold",
            str(gold),
            "--output",
            str(out),
            "--run",
            "--paid-embedding",
        ],
    )
    monkeypatch.setattr(
        rag_experiment, "embed_texts", lambda values, **kw: [[1.0, 0.0] for _ in values]
    )
    assert rag_experiment.main() == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert (
        r["results"][0]["source_complete"]
        and r["source_hash_before"] == r["source_hash_after"]
        and len(r["provider_batches"]) == 2
    )


def test_late_dry_run_never_imports_optional_models_or_calls_cloud(tmp_path, monkeypatch):
    import sys

    from scripts import rag_experiment

    inp = tmp_path / "doc.json"
    out = tmp_path / "late.json"
    inp.write_text(json.dumps(source()), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["exp", "--input", str(inp), "--output", str(out), "--variant", "late"]
    )
    monkeypatch.setattr(
        rag_experiment, "LocalTokenEncoder", lambda *a: pytest.fail("dry late cannot load models")
    )
    assert rag_experiment.main() == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "dry_run_prepared"


def test_source_gold_diagnostic_cross_page_candidate_is_not_false_recall_miss():
    from scripts.rag_eval import Case, score

    case = Case(
        id="trace",
        query="q",
        relevant=[{"document_id": "doc", "pages": [3], "source_block_ids": ["b"]}],
    )
    candidate = {
        "document_id": "doc",
        "page_no": None,
        "pages": [2, 3],
        "chunk_id": "new",
        "source_block_ids": ["a", "b"],
        "diagnostic_candidate": True,
    }
    diag = {
        "diagnostic_version": "rag-retrieval-v2",
        "lanes": {"vector:0": {"candidates": [candidate]}},
        "fusion": [candidate],
        "rerank": [candidate],
    }
    result = score(case, [], diag)
    assert (
        result["failure_stage"] == "context_or_top_k" and result["stage_unit_recall"]["vector"] == 1
    )


def test_experiment_cosine_overflow_fails_instead_of_fake_zero_similarity():
    with pytest.raises(ValueError):
        cosine([1e308, 1e308], [1e308, 1e308])


def test_late_forward_has_trace_for_success_and_failure_without_cloud_price(monkeypatch):
    from app.rag.experiments import late

    obj = object.__new__(LocalTokenEncoder)
    obj.trace_id = "fixture-late"
    obj.tenant_id = "fixture"
    obj.model_identity = {
        "path": "local-token-model",
        "model_and_tokenizer_manifest_hash": "model-hash",
    }
    traces = []
    monkeypatch.setattr(late, "record_trace", lambda **kwargs: traces.append(kwargs))
    monkeypatch.setattr(obj, "_encode", lambda text: token_artifact())
    assert obj.encode("abcd")["kind"] == "contextual_token_hidden_states"
    assert (
        traces[0]["stage"] == "rag_late_local"
        and traces[0]["cost"] == 0
        and not traces[0]["usage_reported"]
    )

    def fail(text):
        raise ValueError("local context too long")

    monkeypatch.setattr(obj, "_encode", fail)
    with pytest.raises(ValueError):
        obj.encode("bad")
    assert traces[-1]["success"] is False and traces[-1]["output_data"]["local_compute_unpriced"]


def test_structural_rejected_relation_changes_chunk_plan_without_fee():
    from app.rag.structure import build_structure

    d = source()
    plan = build_structure(d)
    edge = next(e for e in plan["edges"] if e["from"] == "block:3" and e["to"] == "block:5")
    refused = build_structure(d, rejected_edge_ids=[edge["id"]])
    chunks, diag = split_structural(d, config(), refused)
    assert not any("or表示" in c.content and "Hurry up" in c.content for c in chunks)
    assert diag.plan_signature != split_structural(d, config(), plan)[1].plan_signature


def test_structural_rebuild_and_legacy_rollback_do_not_mix_chunk_versions(db):
    index_document(
        db,
        "教材",
        "Fixture",
        source(),
        tenant_id="owner",
        chunk_layout="legacy",
        meta={"index_revision": 1},
    )
    doc = db.query(KnowledgeDocument).one()
    index_document(
        db,
        "教材",
        "Fixture",
        source(),
        tenant_id="owner",
        chunk_layout="structure",
        document_id=doc.id,
        replace_existing=True,
        meta={"index_revision": 2},
    )
    assert {c.chunker_version for c in db.query(KnowledgeChunk)} == {VERSION}
    index_document(
        db,
        "教材",
        "Fixture",
        source(),
        tenant_id="owner",
        chunk_layout="legacy",
        document_id=doc.id,
        replace_existing=True,
        meta={"index_revision": 3},
    )
    db.refresh(doc)
    assert doc.meta["chunk_layout"] == "legacy" and doc.meta["index_revision"] == 3
    assert {c.chunker_version for c in db.query(KnowledgeChunk)} == {"rag-chunker-v2"}


def test_structural_ocr_result_remembers_explicit_layout_after_index(db, tmp_path, monkeypatch):
    rt = runtime.__wrapped__(db, tmp_path, monkeypatch)
    jid = ready(db, rt)
    p = plan(db, jid, rt[1], chunk_layout="structure")
    body = ReviewApproval(
        preview_hash=p["preview_hash"],
        plan_hash=p["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        chunk_layout="structure",
    )
    approve(db, jid, body, rt[1])
    run_index(jid, rt[2])
    summary = rt[0].summary(db.get(OcrJob, jid))
    assert summary["review_settings"]["chunk_layout"] == "structure"


def test_late_encoder_local_only_token_offsets_and_manifest_without_install(tmp_path, monkeypatch):
    import contextlib

    from app.rag.experiments import late

    class Tensor:
        def __init__(self, value):
            self.value = value

        def __getitem__(self, index):
            return Tensor(self.value[index])

        def tolist(self):
            return self.value

        def float(self):
            return self

        def cpu(self):
            return self

    path = tmp_path / "explicit-model"
    path.mkdir()
    (path / "config.json").write_text('{"max_position_embeddings":8}')
    (path / "model.safetensors").write_bytes(b"fixture-not-real-model")
    calls = []

    class Tokenizer:
        is_fast = True
        model_max_length = 8

        def __call__(self, text, **kwargs):
            return {
                "input_ids": Tensor([[1, 2, 3, 4]]),
                "attention_mask": Tensor([[1, 1, 1, 1]]),
                "offset_mapping": Tensor([[[0, 1], [1, 2], [2, 3], [3, 4]]]),
            }

    class Model:
        config = SimpleNamespace(max_position_embeddings=8)

        def eval(self):
            return self

        def __call__(self, **kwargs):
            return SimpleNamespace(
                last_hidden_state=Tensor([[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]])
            )

    def load_tokenizer(directory, **kwargs):
        calls.append(kwargs)
        return Tokenizer()

    def load_model(directory, **kwargs):
        calls.append(kwargs)
        return Model()

    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
        AutoModel=SimpleNamespace(from_pretrained=load_model),
    )
    torch = SimpleNamespace(
        inference_mode=contextlib.nullcontext, ones_like=lambda tensor: Tensor([[1, 1, 1, 1]])
    )
    monkeypatch.setattr(
        late.importlib,
        "import_module",
        lambda name: transformers if name == "transformers" else torch,
    )
    monkeypatch.setattr(late, "record_trace", lambda **kwargs: None)
    obj = LocalTokenEncoder(path, max_tokens=8)
    artifact = obj.encode("abcd")
    assert (
        artifact["kind"] == "contextual_token_hidden_states" and obj.model_identity["file_manifest"]
    )
    assert all(c["local_files_only"] and c["trust_remote_code"] is False for c in calls)
    assert calls[1]["use_safetensors"]
    assert pool_tokens("abcd", artifact, [[(0, 2)]]) == [[1.0, 0.0]]
    obj.maximum = 2
    with pytest.raises(ValueError):
        obj.encode("abcd")


def test_native_file_preview_and_upload_layout_are_consistent_without_hidden_default(
    db, monkeypatch
):
    service = KnowledgeService(db)
    body = b"# Topic\n\nNative paragraph. Second sentence."
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda *a, **k: pytest.fail("preview is free")
    )
    preview = service.preview_file("native.md", body, chunk_layout="structure")
    assert preview["chunk_layout"] == "structure" and preview["chunks"][0]["source_segments"]
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values],
    )
    result = service.upload_file("native.md", body, "教材", None, actor(), chunk_layout="structure")
    assert (
        result["diagnostics"]["layout"] == "structure"
        and db.query(KnowledgeDocument).one().meta["chunk_layout"] == "structure"
    )


def test_upload_schema_rejects_unknown_layout_before_fee():
    from pydantic import ValidationError

    from app.schemas import KnowledgeUploadIn

    with pytest.raises(ValidationError):
        KnowledgeUploadIn(
            source_type="教材", source_name="fixture", text="body", chunk_layout="semantic"
        )
