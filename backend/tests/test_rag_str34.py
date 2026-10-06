"""STR3/4: table/ref/coverage/window bounds and negative examples, no paid providers."""

from __future__ import annotations

import copy
import io
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.errors import InvalidKnowledgeError, TenantScopeDeniedError
from app.models import KnowledgeChunk, KnowledgeDocument, OcrBoundaryJob, OcrJob
from app.rag import retriever
from app.rag.indexer import index_document
from app.rag.ocr.mineru import NativeWindowDocument, OcrParseError, validate_window
from app.rag.parser import _block, _finish, parse_text
from app.rag.retrieval.models import Candidate
from app.rag.retrieval.planning import coverage_order, question_parts
from app.rag.retrieval.table_context import logical_views
from app.rag.structure import build_structure
from app.rag.tables import expand_html_table
from app.services.boundary_service import BoundaryService, WindowRequest
from app.services.ocr_service import now
from app.versioning import hash_value
from app.worker.boundary import run_boundary, sweep_boundaries
from tests.test_ocr_review import ready, runtime
from tests.test_rag_ocr import native, pdf


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_PARENT_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "rules")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts",
        lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values],
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda values, **kwargs: [[1.0] + [0.0] * 1023 for _ in values]
    )
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kwargs: None)


def table(text, page, continued=False, caption="", bbox=None):
    parsed = expand_html_table(text)
    parsed.pop("warnings")
    return _block(
        "table",
        parsed.pop("text"),
        page=page,
        locator={
            "bbox": bbox or ([0.1, 0.65, 0.9, 0.98] if page == 1 else [0.1, 0.02, 0.9, 0.3]),
            "bbox_units": "normalized_page",
            "bbox_frame": "mineru_rendered_page",
        },
        meta={
            **parsed,
            "table_id": f"p{page}-t",
            "native_type": "table_body",
            "table_continues_prev": continued,
            "caption": caption,
        },
    )


def source(strong=True, missing_header=False):
    first = "<table><tr><th>Word</th><th>Meaning</th></tr><tr><td>A</td><td>first part</td></tr></table>"
    second = (
        "<table>"
        + ("" if missing_header else "<tr><th>Word</th><th>Meaning</th></tr>")
        + "<tr><td>B</td><td>continuation</td></tr></table>"
    )
    return _finish(
        "fixture.pdf",
        [
            _block("heading", "第一章 Rules", level=1, page=1),
            table(first, 1, caption="Table 1"),
            table(second, 2, continued=strong, caption="Table 1 continued" if strong else ""),
        ],
        [],
    )


def who(tenant="own"):
    return SimpleNamespace(id=None, role="researcher", tenant_id=tenant, status="active")


def persisted(db, document, decisions=None):
    index_document(db, "教材", "Fixture", document, tenant_id="own", structure_decisions=decisions)
    doc = db.query(KnowledgeDocument).filter_by(tenant_id="own").one()
    return doc, list(
        db.query(KnowledgeChunk).filter_by(document_id=doc.id).order_by(KnowledgeChunk.chunk_index)
    )


def test_strong_multi_evidence_table_is_logical_group_and_source_untouched():
    doc = source()
    original = copy.deepcopy(doc)
    p = build_structure(doc)
    assert doc == original
    edge = next(e for e in p["edges"] if e["relation"] == "table_continues")
    assert edge["state"] == "accepted" and "aligned_page_bottom_top" in edge["evidence"]
    assert len(p["logical_tables"]) == 1 and len(p["logical_tables"][0]["physical_tables"]) == 2
    assert all(
        e["state"] == "proposed" for e in p["edges"] if e["relation"] == "table_cell_continues"
    )


@pytest.mark.parametrize(
    "damage",
    [
        "weak",
        "bbox_missing",
        "alignment",
        "new_chapter",
        "gap",
        "excluded",
        "columns",
        "intervening_body",
        "header_difference",
    ],
)
def test_table_similarity_alone_never_justifies_automatic_merge(damage):
    d = source()
    if damage == "weak":
        d["blocks"][2]["meta"].update(table_continues_prev=False, caption="")
    if damage == "bbox_missing":
        d["blocks"][2]["source_locator"] = {}
    if damage == "alignment":
        d["blocks"][2]["source_locator"]["bbox"] = [0.3, 0.02, 0.95, 0.3]
    if damage == "gap":
        d["blocks"][2]["page_no"] = 4
    if damage == "excluded":
        d["blocks"][2]["order"] = 5
    if damage == "columns":
        d["blocks"][2]["meta"]["rows"] = [["X", "Y", "Z"]]
    if damage == "header_difference":
        d["blocks"][2]["meta"]["rows"][0] = ["Other", "Head"]
        d["blocks"][2]["meta"]["caption"] = ""
    if damage in {"new_chapter", "intervening_body"}:
        b = (
            _block("heading", "第二章 Other", level=1, page=2)
            if damage == "new_chapter"
            else _block("paragraph", "Unrelated explanation.", page=2)
        )
        d = _finish("fixture.pdf", [*d["blocks"][:2], b, d["blocks"][2]], [])
    p = build_structure(d)
    assert not any(
        e["relation"] == "table_continues" and e["state"] == "accepted" for e in p["edges"]
    )


def test_no_header_continuation_is_proposed_review_can_confirm_without_guessing_header():
    d = source(strong=False, missing_header=True)
    p = build_structure(d)
    t = next(e for e in p["edges"] if e["relation"] == "table_continues")
    assert t["state"] == "proposed"
    reviewed = build_structure(d, accepted_edge_ids=[t["id"]])
    assert len(reviewed["logical_tables"]) == 1
    assert not reviewed["logical_tables"][0]["physical_tables"][1]["header_confirmed"]


def test_cell_confirmation_is_per_column_and_requires_table_confirmation():
    d = source(strong=False)
    p = build_structure(d)
    te = next(e for e in p["edges"] if e["relation"] == "table_continues")
    ce = next(e for e in p["edges"] if e["relation"] == "table_cell_continues")
    with pytest.raises(ValueError):
        build_structure(d, accepted_edge_ids=[ce["id"]])
    confirmed = build_structure(d, accepted_edge_ids=[te["id"], ce["id"]])
    assert (
        sum(
            e["state"] == "accepted"
            for e in confirmed["edges"]
            if e["relation"] == "table_cell_continues"
        )
        == 1
    )


def test_logical_view_uses_delivered_sources_dedups_header_and_keeps_cell_parts(db):
    d = source()
    p = build_structure(d)
    ce = next(
        e for e in p["edges"] if e["relation"] == "table_cell_continues" and e["columns"] == [1, 2]
    )
    te = next(e for e in p["edges"] if e["relation"] == "table_continues")
    doc, rows = persisted(db, d, {"accepted_edge_ids": [te["id"], ce["id"]]})
    details = []
    info = {}
    texts = retriever.retrieve(
        db,
        "first part",
        tenant_id="own",
        top_k=1,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert texts and not info["fallbacks"]
    views = info["context_bundles"][0]["logical_table_views"]
    v = views[0]
    assert len(v["header_sources"]) == 2 and v["rendered_table"].count("Word") == 1
    assert ce["id"] in v["confirmed_cell_joins"]
    merged = next(c for row in v["rows"] for c in row["cells"] if c.get("derived_join"))
    assert "first part continuation" == merged["text"] and {
        part["page_no"] for part in merged["parts"]
    } == {1, 2}
    assert all(
        s["content"] == doc.normalized_text[s["content_start"] : s["content_end"]]
        for s in details[0]["source_segments"]
    )


def test_logical_view_cannot_inject_non_delivered_grid_text():
    p = build_structure(source())
    group = p["logical_tables"][0]
    assert logical_views([], [group]) == []


def test_colspan_ranges_are_compared_not_flattened_guess():
    d = source()
    d["blocks"][1]["meta"]["spans"] = [
        {"row": 0, "column": 0, "header": True, "rowspan": 1, "colspan": 2},
        {"row": 1, "column": 0, "header": False, "rowspan": 1, "colspan": 2},
    ]
    p = build_structure(d)
    assert not any(e["relation"] == "table_cell_continues" for e in p["edges"])
    assert p["logical_tables"][0]["unresolved_cell_boundaries"]


def reference_doc(target="第二章"):
    return _finish(
        "refs.md",
        [
            _block("heading", "第一章 一般规则", level=1),
            _block("paragraph", f"General definition. 例外参见{target}。"),
            _block("heading", "第二章 例外", level=1),
            _block("heading", "第一节 特殊用法", level=2),
            _block("paragraph", "Exception applies only to special cases."),
        ],
        [],
    )


def test_explicit_chapter_reference_targets_real_descendant_body_not_heading():
    p = build_structure(reference_doc())
    e = next(e for e in p["edges"] if e["relation"] == "references_section")
    assert e["from"] == "block:1" and e["to"] == "block:4" and e["state"] == "accepted"


def test_explicit_reference_expands_directed_across_chapters_with_original_citations(db):
    _, rows = persisted(db, reference_doc())
    details = []
    info = {}
    retriever.retrieve(
        db,
        "General definition",
        tenant_id="own",
        top_k=1,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert (
        "Exception applies" in details[0]["content"]
        and info["context_bundles"][0]["reference_edges"]
    )


@pytest.mark.parametrize("target", ["第九章", "第三节"])
def test_missing_reference_stays_unresolved_not_invented(target):
    p = build_structure(reference_doc(target))
    assert p["unresolved_references"] and not any(
        e["relation"] == "references_section" for e in p["edges"]
    )


def test_ambiguous_chapter_name_does_not_pick_arbitrary_target():
    d = reference_doc()
    d = _finish(
        "refs.md",
        d["blocks"]
        + [
            _block("heading", "第二章 同名附录", level=1),
            _block("paragraph", "Different meaning."),
        ],
        [],
    )
    p = build_structure(d)
    assert p["unresolved_references"] and not any(
        e["relation"] == "references_section" for e in p["edges"]
    )


def test_question_plan_preserves_complementary_phrases_without_scope_changes():
    d = {}
    parts = question_parts("for和because的句子位置，以及so因果关系的例句是什么？", d, {})
    assert parts == ["for和because的句子位置", "so因果关系的例句是什么"]
    assert d["question_plan"]["scope_unchanged"]


def test_multiple_configured_concepts_have_navigation_and_original_is_not_overwritten():
    d = {}
    parts = question_parts("并列句的结构与动词的谓语作用分别是什么？", d, {})
    assert parts and len(d["question_plan"]["concept_navigation"]) >= 2


def test_noncompound_question_adds_no_paid_query():
    assert question_parts("连系动词为什么需要表语？", {}, {}) == []


def test_rules_planning_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    assert question_parts("A以及B", {}, {}) == []


def test_query_count_and_part_length_boundaries(monkeypatch):
    monkeypatch.setattr(settings, "RAG_QUERY_PLAN_MAX_PARTS", 2)
    assert len(question_parts("first;second;third;fourth", {}, {})) == 2
    assert question_parts("a" * 300 + ";short", {}, {}) == ["short"]


def test_coverage_orders_one_seed_for_each_complement_not_all_from_one_lane():
    a = Candidate(KnowledgeChunk(id="a", content="a"), ranks={"vector:1": 1}, rrf_score=0.3)
    b = Candidate(KnowledgeChunk(id="b", content="b"), ranks={"vector:2": 1}, rrf_score=0.2)
    c = Candidate(KnowledgeChunk(id="c", content="c"), ranks={"vector:1": 2}, rrf_score=0.25)
    d = {}
    assert [x.row.id for x in coverage_order([a, c, b], [1, 2], d)][:2] == ["a", "b"]


@pytest.fixture
def window_runtime(db, tmp_path, monkeypatch):
    rt = runtime.__wrapped__(db, tmp_path, monkeypatch)
    from tests.test_ocr_jobs import FakeClient

    service, actor, factory = rt
    job = service.create(io.BytesIO(pdf(["scan", "scan", "scan"])), "course.pdf", actor, None)
    from app.worker.ocr_runner import run_job

    for _ in range(3):
        run_job(job["id"], factory=factory, client_factory=FakeClient, dispatch=lambda _: None)
    return job["id"], actor, factory


class WindowClient:
    calls = 0

    def __init__(self, **kwargs):
        self.cancel = kwargs.get("cancel_check")

    def server_version(self):
        return "4.0.10"

    def parse_window(self, content, expected_pages, filename="window.pdf"):
        self.__class__.calls += 1
        raw = native()
        raw["pages"] = [
            {"page_idx": i, "blocks": copy.deepcopy(raw["pages"][0]["blocks"])}
            for i in range(expected_pages)
        ]
        return validate_window(raw, expected_pages)


def test_durable_window_local_to_global_and_original_checkpoints_unchanged(db, window_runtime):
    identifier, actor, factory = window_runtime
    svc = BoundaryService(db, lambda _: None)
    original = db.get(OcrJob, identifier)
    old = (original.preview_hash, original.preview_key, original.index_status)
    j = svc.create(identifier, WindowRequest(pages=[2, 3], local_compute_acknowledged=True), actor)
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "completed"
    result = svc.result(j["id"], actor)
    assert (
        result["local_to_global"] == {"0": 2, "1": 3}
        and result["global_pages"] == [2, 3]
        and not result["source_applied"]
    )
    db.expire_all()
    original = db.get(OcrJob, identifier)
    assert (original.preview_hash, original.preview_key, original.index_status) == old
    assert db.query(KnowledgeChunk).count() == 0
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "not_claimed"


def test_window_three_pages_and_duplicate_create_is_idempotent(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    body = WindowRequest(pages=[1, 2, 3], local_compute_acknowledged=True)
    a = s.create(identifier, body, actor)
    b = s.create(identifier, body, actor)
    assert a["id"] == b["id"]
    assert run_boundary(a["id"], factory, WindowClient)["status"] == "completed"


@pytest.mark.parametrize(
    "pages,ack", [([1, 3], True), ([3, 4], True), ([2, 1], True), ([1, 2], False)]
)
def test_window_rejects_gaps_out_of_range_and_missing_ack(db, window_runtime, pages, ack):
    identifier, actor, _ = window_runtime
    with pytest.raises(InvalidKnowledgeError):
        BoundaryService(db, lambda _: None).create(
            identifier, WindowRequest(pages=pages, local_compute_acknowledged=ack), actor
        )


def test_window_cancel_resume_and_lease_recovery(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    assert s.cancel(j["id"], actor)["status"] == "cancelled"
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "not_claimed"
    assert s.resume(j["id"], actor)["status"] == "pending"
    row = db.get(OcrBoundaryJob, j["id"])
    row.status = "running"
    row.lease_until = now() - timedelta(seconds=1)
    row.attempts = 1
    db.commit()
    sent = []
    assert sweep_boundaries(factory, lambda x: sent.append(x))[
        "boundary_recovered"
    ] == 1 and sent == [row.id]
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "completed"


def test_window_tenant_and_stale_preview_protected(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    with pytest.raises(TenantScopeDeniedError):
        s.get(j["id"], who("foreign"))
    parent = db.get(OcrJob, identifier)
    parent.preview_hash = "0" * 64
    db.commit()
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "failed"


def test_window_engine_failure_is_durable_no_mock_success(db, window_runtime):
    class Broken(WindowClient):
        def parse_window(self, *a, **k):
            raise OcrParseError("poll", "failure")

    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    assert run_boundary(j["id"], factory, Broken)["status"] == "failed"
    assert s.get(j["id"], actor).error and s.get(j["id"], actor).result_key is None


@pytest.mark.parametrize("bad", ["missing", "out_of_order", "partial", "duplicate_index"])
def test_window_protocol_requires_complete_valid_native_pages(bad):
    raw = native()
    raw["pages"] = [raw["pages"][0], copy.deepcopy(raw["pages"][0])]
    raw["pages"][1]["page_idx"] = 1
    if bad == "missing":
        raw["pages"].pop()
    if bad == "out_of_order":
        raw["pages"][1]["page_idx"] = 2
    if bad == "partial":
        raw["is_full_document"] = False
    if bad == "duplicate_index":
        raw["pages"][1]["blocks"].append(copy.deepcopy(raw["pages"][1]["blocks"][0]))
    with pytest.raises(OcrParseError):
        validate_window(raw, 2)


def test_window_cache_is_version_source_pages_and_tenant_bound(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    assert run_boundary(j["id"], factory, WindowClient)["status"] == "completed"
    calls = WindowClient.calls
    row = db.get(OcrBoundaryJob, j["id"])
    row.status = "failed"
    db.commit()
    s.resume(j["id"], actor)
    result = run_boundary(j["id"], factory, WindowClient)
    assert result["cache_hit"] and WindowClient.calls == calls


def test_window_read_result_hash_and_preview_drift_reject(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    run_boundary(j["id"], factory, WindowClient)
    row = db.get(OcrBoundaryJob, j["id"])
    row.result_hash = "0" * 64
    db.commit()
    with pytest.raises(InvalidKnowledgeError):
        s.result(j["id"], actor)
    parent = db.get(OcrJob, identifier)
    parent.preview_hash = "1" * 64
    db.commit()
    with pytest.raises(InvalidKnowledgeError):
        s.result(j["id"], actor)


def test_expired_last_window_attempt_marks_failed_not_infinite_redelivery(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)
    row = db.get(OcrBoundaryJob, j["id"])
    row.status = "running"
    row.attempts = settings.RAG_OCR_BOUNDARY_MAX_ATTEMPTS
    row.lease_until = now() - timedelta(seconds=1)
    db.commit()
    sent = []
    sweep_boundaries(factory, lambda x: sent.append(x))
    db.expire_all()
    assert db.get(OcrBoundaryJob, j["id"]).status == "failed" and not sent


def test_worker_cancel_fences_multi_page_result(db, window_runtime):
    identifier, actor, factory = window_runtime
    s = BoundaryService(db, lambda _: None)
    j = s.create(identifier, WindowRequest(pages=[1, 2], local_compute_acknowledged=True), actor)

    class Cancel(WindowClient):
        def parse_window(self, *args, **kwargs):
            s.cancel(j["id"], actor)
            return super().parse_window(*args, **kwargs)

    assert run_boundary(j["id"], factory, Cancel)["status"] == "stale"
    db.expire_all()
    assert db.get(OcrBoundaryJob, j["id"]).status == "cancelled"


def test_llm_question_plan_schema_failure_has_trace_and_rules_fallback(monkeypatch):
    from app.rag.retrieval import planning

    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "llm")
    traces = []
    monkeypatch.setattr(planning, "record_trace", lambda **kwargs: traces.append(kwargs))

    class Fake:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"parts":[12]}'))],
            )

    monkeypatch.setattr(planning, "_client", Fake)
    d = {}
    assert planning.question_parts("first以及second", d, {"trace_id": "fixture"}) == [
        "first",
        "second",
    ]
    assert (
        d["fallbacks"][0]["stage"] == "question_plan"
        and traces[0]["stage"] == "rag_plan"
        and traces[0]["success"] is False
    )


def test_retrieval_preserves_scope_and_original_query_with_complements(db, monkeypatch):
    doc, rows = persisted(db, reference_doc())
    calls = []
    monkeypatch.setattr(
        retriever,
        "embed_texts",
        lambda values, **kwargs: calls.append(values) or [[1.0] + [0.0] * 1023 for _ in values],
    )
    citations = []
    info = {}
    q = "General definition以及Exception applies"
    retriever.retrieve(
        db,
        q,
        tenant_id="own",
        document_ids=[doc.id],
        top_k=2,
        context_mode="relation",
        details=citations,
        diagnostics=info,
    )
    assert calls[0][0] == q and calls[0][1:3] == ["General definition", "Exception applies"]
    assert info["question_plan"]["scope_unchanged"] and info["coverage_selection"]["assignments"]
    assert all(c["document_id"] == doc.id for c in citations)


def test_explicit_reference_section_filter_cannot_be_bypassed(db):
    doc, rows = persisted(db, reference_doc())
    details = []
    info = {}
    retriever.retrieve(
        db,
        "General definition",
        tenant_id="own",
        section_path=["第一章 一般规则"],
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert details and "Exception applies" not in details[0]["content"]


def test_conflicting_sources_are_separate_bundles_unverified_not_reconciled(db):
    from app.rag.parser import parse_text

    index_document(
        db, "教材", "Source A", parse_text("a.md", "# Rule\n\nAnswer is A."), tenant_id="own"
    )
    index_document(
        db, "教材", "Source B", parse_text("b.md", "# Rule\n\nAnswer is B."), tenant_id="own"
    )
    citations = []
    retriever.retrieve(
        db, "Answer", tenant_id="own", top_k=2, context_mode="relation", details=citations
    )
    assert len({c["document_id"] for c in citations}) == 2
    assert all(c["verification"] == "unverified" for c in citations)


def test_unknown_bbox_coordinate_frame_cannot_auto_join():
    d = source()
    d["blocks"][2]["source_locator"]["bbox_units"] = "points"
    p = build_structure(d)
    assert all(e["state"] == "proposed" for e in p["edges"] if e["relation"] == "table_continues")


def test_multiple_question_budget_reserves_space_for_other_source(db, monkeypatch):
    from app.rag.knowledge_points import load_catalog
    from app.rag.retrieval.relation import expand_relations

    a = parse_text("a.md", "# Big rule\n\n" + ("First long fact. " * 200))
    b = parse_text("b.md", "# Other rule\n\nSecond independent fact.")
    index_document(db, "教材", "A", a, tenant_id="own")
    index_document(db, "教材", "B", b, tenant_id="own")
    rows = list(db.query(KnowledgeChunk))
    first = next(r for r in rows if r.source_name == "A")
    second = next(r for r in rows if r.source_name == "B")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MAX_CHARS", 1024)
    info = {"question_plan": {"parts": ["first", "second"]}}
    texts, citations = expand_relations(
        db,
        [Candidate(first), Candidate(second)],
        load_catalog().scope(None, "exact"),
        "own",
        False,
        2,
        info,
    )
    assert (
        len(citations) == 2
        and "Second independent fact" in " ".join(texts)
        and len("\n\n".join(texts)) <= 1024
    )


def test_logical_cell_metric_requires_actual_rendered_sources_not_claimed_map(db):
    from scripts.rag_eval import Case, score

    d = source()
    p = build_structure(d)
    edge = next(
        e for e in p["edges"] if e["relation"] == "table_cell_continues" and e["columns"] == [1, 2]
    )
    doc, rows = persisted(db, d, {"accepted_edge_ids": [edge["id"]]})
    details = []
    info = {}
    retriever.retrieve(
        db,
        "first part",
        tenant_id="own",
        top_k=1,
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    expected = Case(
        id="cell",
        query="first part",
        top_k=1,
        relevant=[
            {
                "document_id": doc.id,
                "pages": [1, 2],
                "required_logical_rows": [["first part", "continuation"]],
            }
        ],
    )
    result = score(expected, details, info)
    assert result["logical_row_support"] and result["context_complete"]
    bad = copy.deepcopy(info)
    bad["context_bundles"][0]["logical_table_views"][0]["source_segment_ids"].append("not-sent")
    assert not score(expected, details, bad)["logical_row_support"]
    bad = copy.deepcopy(info)
    bad["context_bundles"][0]["logical_table_views"][0]["context_delivered"] = False
    assert not score(expected, details, bad)["logical_row_support"]
    bad = copy.deepcopy(info)
    row = bad["context_bundles"][0]["logical_table_views"][0]["rows"][0]
    row["cells"][1]["text"] = "Fabricated first part continuation"
    assert not score(expected, details, bad)["logical_row_support"]


def test_reference_unavailable_explicit_scope_reports_missing_member(db):
    doc, rows = persisted(db, reference_doc())
    details = []
    info = {}
    retriever.retrieve(
        db,
        "General definition",
        tenant_id="own",
        section_path=["第一章 一般规则"],
        context_mode="relation",
        details=details,
        diagnostics=info,
    )
    assert details and "scope_or_active_member_missing" in details[0]["incomplete_reasons"]


def test_boundary_routes_require_permission_and_enforce_parent_job(db, window_runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.api import ocr_routes
    from app.database import get_db
    from app.errors import PlatformError
    from app.security import get_current_user

    identifier, actor, factory = window_runtime
    original_service = BoundaryService
    monkeypatch.setattr(
        ocr_routes, "BoundaryService", lambda session: original_service(session, lambda _: None)
    )
    application = FastAPI()
    application.include_router(ocr_routes.router)
    application.add_exception_handler(
        PlatformError,
        lambda request, error: JSONResponse({"detail": error.code}, status_code=error.status_code),
    )
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role="viewer", status="active", tenant_id=actor.tenant_id
    )
    client = TestClient(application)
    assert client.get(f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews").status_code == 403
    application.dependency_overrides[get_current_user] = lambda: actor
    response = client.post(
        f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews",
        json={"pages": [1, 2], "local_compute_acknowledged": True},
    )
    assert response.status_code == 202
    bid = response.json()["id"]
    assert (
        client.get(f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews").json()["items"][0][
            "id"
        ]
        == bid
    )
    assert (
        client.get(f"/api/knowledge/ocr/jobs/other-job/boundary-reviews/{bid}").status_code
        == InvalidKnowledgeError.status_code
    )
    assert (
        client.get(
            f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews/{bid}/result"
        ).status_code
        == InvalidKnowledgeError.status_code
    )
    assert (
        client.post(f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews/{bid}/cancel").json()[
            "status"
        ]
        == "cancelled"
    )
    assert (
        client.post(f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews/{bid}/resume").json()[
            "status"
        ]
        == "pending"
    )
    assert (
        client.post(
            f"/api/knowledge/ocr/jobs/{identifier}/boundary-reviews",
            json={"pages": [1, 3], "local_compute_acknowledged": True},
        ).status_code
        == InvalidKnowledgeError.status_code
    )


def test_scheduler_task_recovers_both_single_pages_and_windows(monkeypatch):
    from app.worker import boundary, ocr_tasks

    monkeypatch.setattr(ocr_tasks, "sweep", lambda: {"recovered": 0, "sent": 0})
    monkeypatch.setattr(
        boundary, "sweep_boundaries", lambda: {"boundary_recovered": 1, "boundary_sent": 2}
    )
    result = ocr_tasks.sweep_ocr_jobs.run()
    assert result["boundary_recovered"] == 1 and result["boundary_sent"] == 2


def test_matching_colspans_are_per_logical_range_not_cell_position():
    d = source()
    for block in d["blocks"][1:]:
        block["meta"]["spans"] = [
            {"row": 0, "column": 0, "header": True, "rowspan": 1, "colspan": 2},
            {"row": 1, "column": 0, "header": False, "rowspan": 1, "colspan": 2},
        ]
    p = build_structure(d)
    candidates = [e for e in p["edges"] if e["relation"] == "table_cell_continues"]
    assert (
        len(candidates) == 1
        and candidates[0]["columns"] == [0, 2]
        and candidates[0]["state"] == "proposed"
    )


def test_single_native_endpoint_stays_single_and_window_checks_page_count(monkeypatch):
    from app.rag.ocr.mineru import MinerUClient

    client = MinerUClient(url="http://localhost:16580")
    raw = native()
    monkeypatch.setattr(client, "_parse", lambda content, filename, count: raw)
    assert len(client.parse_page(b"fixture").pages) == 1
    with pytest.raises(OcrParseError):
        client.parse_window(b"fixture", 2)
    with pytest.raises(OcrParseError):
        client.parse_window(b"fixture", 4)


def test_unknown_header_and_duplicate_header_have_explicit_provenance(db):
    d = source(missing_header=True)
    p = build_structure(d)
    edge = next(e for e in p["edges"] if e["relation"] == "table_continues")
    doc, rows = persisted(db, d, {"accepted_edge_ids": [edge["id"]]})
    citations = []
    info = {}
    retriever.retrieve(
        db,
        "continuation",
        tenant_id="own",
        top_k=1,
        context_mode="relation",
        details=citations,
        diagnostics=info,
    )
    view = info["context_bundles"][0]["logical_table_views"][0]
    assert len(view["physical_provenance"]) == 2
    assert any("B" == cell["text"] for row in view["rows"] for cell in row["cells"])
    assert view["complete_physical_segments"]
