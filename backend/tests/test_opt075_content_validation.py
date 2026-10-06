"""Format guards and semantic limitations: synthetic examples, no provider calls."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from app.engine.content_validation import (
    enforce_content,
    inspect_content,
    load_content_rules,
)
from app.engine.structured_output import generate_structured
from app.errors import ContentStateConflictError, ContentValidationError, StructuredOutputError
from app.models import ContentItem, GenerationTask, QualityRecord, QuestionTemplate
from app.schemas import QualityReviewRequest
from app.services.content_service import ContentService
from app.services.quality_service import QualityService
from app.template_loader import _validate_template
from app.workflow import graph as wf

ROOT = Path(__file__).resolve().parents[1]


def template_data(name="cloze"):
    return yaml.safe_load((ROOT / "app/templates" / f"{name}.yaml").read_text(encoding="utf-8"))


def payload(name="cloze", count=5):
    q = {
        "options": ["so", "and", "but", "or"],
        "answer": "A",
        "explanation": "Explain all options.",
    }
    if name == "single_choice":
        return {"stem": "It was raining, ____ we stayed indoors.", **q}
    if name == "reading":
        return {
            "passage": "It was raining.",
            "questions": [{"stem": f"Question {i}", **copy.deepcopy(q)} for i in range(count)],
        }
    return {
        "passage": " ".join("A sentence ____ follows." for _ in range(count)),
        "blanks": [{"index": i + 1, **copy.deepcopy(q)} for i in range(count)],
    }


def inspect(value, name="cloze"):
    t = template_data(name)
    return inspect_content(value, t["output_schema"], t["run_config"])


@pytest.mark.parametrize("name", ["cloze", "reading", "single_choice"])
def test_builtin_valid_and_nonmutating(name):
    t = template_data(name)
    value = payload(name)
    before = copy.deepcopy(value)
    assert enforce_content(value, t["output_schema"], t["run_config"]).valid
    assert value == before
    assert _validate_template(t) == []
    assert t["version"] == 3


@pytest.mark.parametrize(
    "options,code",
    [
        (["a", "b", "c"], "option_count"),
        (["a", "b", "c", "d", "e"], "option_count"),
        (["", "b", "c", "d"], "empty_option"),
        (["  ", "b", "c", "d"], "empty_option"),
        (["a", "a", "c", "d"], "duplicate_option"),
        ([" Hello  there ", "hello there", "c", "d"], "duplicate_option"),
        (["Ａnd", "and", "c", "d"], "duplicate_option"),
        (["A. and", "B. but", "C. so", "D. or"], "option_label"),
        (["A．and", "but", "so", "or"], "option_label"),
        ([None, "but", "so", "or"], "empty_option"),
    ],
)
@pytest.mark.parametrize("name", ["cloze", "reading", "single_choice"])
def test_choice_invariants_all_builtin(name, options, code):
    value = payload(name)
    q = value if name == "single_choice" else value["blanks" if name == "cloze" else "questions"][0]
    q["options"] = options
    report = inspect(value, name)
    assert not report.valid
    assert code in {e.code for e in report.errors}


@pytest.mark.parametrize("answer", ["and", "E", "a", "A.", " A", "", None, 0])
def test_answer_is_canonical_and_maps_to_option(answer):
    value = payload()
    value["blanks"][0]["answer"] = answer
    assert "answer_label" in {e.code for e in inspect(value).errors}


@pytest.mark.parametrize(
    "indices",
    [
        [0, 2, 3, 4, 5],
        [1, 1, 3, 4, 5],
        [1, 3, 2, 4, 5],
        [1, 2, 3, 4, 6],
        [True, 2, 3, 4, 5],
        ["1", 2, 3, 4, 5],
    ],
)
def test_indices_are_ordered_contiguous_positive_integers(indices):
    value = payload()
    for q, index in zip(value["blanks"], indices):
        q["index"] = index
    assert "index_sequence" in {e.code for e in inspect(value).errors}


@pytest.mark.parametrize(
    "passage,code",
    [
        ("No gaps", "blank_count"),
        ("____ ____", "blank_count"),
        ("_____ ____ ____ ____ ____", "blank_marker"),
        ("___ ____ ____ ____ ____", "blank_marker"),
        ("__ ____ ____ ____ ____ ____", "blank_marker"),
        (None, "blank_count"),
    ],
)
def test_passage_gap_count_and_marker(passage, code):
    value = payload()
    value["passage"] = passage
    assert code in {e.code for e in inspect(value).errors}


@pytest.mark.parametrize(
    "value",
    [
        {"passage": "x", "blanks": []},
        {"passage": "x", "blanks": [None]},
        {"passage": "x", "blanks": None},
    ],
)
def test_missing_empty_and_nonobject_blanks(value):
    assert not inspect(value).valid


def test_all_a_warning_is_not_blocker_or_shuffle():
    value = payload()
    report = inspect(value)
    assert report.valid and report.warnings[0].code == "answer_position_bias"
    assert [q["answer"] for q in value["blanks"]] == ["A"] * 5
    assert not inspect(payload(count=2)).warnings
    value["blanks"][1]["answer"] = "B"
    assert not inspect(value).warnings


def test_deterministic_pass_never_proves_unique_semantic_answer():
    # "and" is also plausible; rules must not advertise semantic uniqueness.
    report = inspect(payload("single_choice"), "single_choice")
    assert report.valid
    assert report.semantic_verification == "requires_human_review"


def test_new_type_uses_config_without_python_type_dispatch():
    value = {"section": {"choices": [{"variants": ["one", "two"], "key": "B"}]}}
    rules = {
        "content_validation": {
            "choice_sets": [
                {
                    "path": "section.choices",
                    "options_field": "variants",
                    "answer_field": "key",
                    "labels": ["A", "B"],
                }
            ]
        }
    }
    assert enforce_content(value, {}, rules).valid
    value["section"]["choices"][0]["key"] = "one"
    with pytest.raises(ContentValidationError):
        enforce_content(value, {}, rules)


@pytest.mark.parametrize(
    "rule",
    [
        {"labels": ["A", "A"]},
        {"labels": ["a", "B"]},
        {"labels": ["AA", "B"]},
        {"path": ".bad"},
        {"path": "a..b"},
        {"blank_marker": ""},
        {"passage_field": "passage"},
        {"options_field": ""},
        {"typo": True},
        {"position_warning_min": 1},
    ],
)
def test_bad_rule_config_rejected_on_import(rule):
    data = template_data()
    data["run_config"]["content_validation"]["choice_sets"] = [rule]
    assert any("content_validation" in e for e in _validate_template(data))
    with pytest.raises(ValidationError):
        load_content_rules(data["run_config"])


def setup_engine(monkeypatch, values):
    t = template_data()
    t["run_config"]["max_retry"] = 2
    messages, trace = [], Mock()

    def call(_client, _model, current):
        messages.append(copy.deepcopy(current))
        return (
            json.dumps(values.pop(0)),
            5.0,
            SimpleNamespace(prompt_tokens=20, completion_tokens=10),
        )

    monkeypatch.setattr("app.engine.structured_output._get_openai_client", lambda: None)
    monkeypatch.setattr("app.engine.structured_output._call_openai_json", call)
    monkeypatch.setattr("app.engine.structured_output.record_trace", trace)
    return SimpleNamespace(**t), messages, trace


def test_content_retry_feedback_cost_and_warning_trace(monkeypatch):
    bad = payload()
    bad["blanks"][1]["index"] = 1
    template, messages, trace = setup_engine(monkeypatch, [bad, payload()])
    result = generate_structured(template, {"quantity": 1}, None, trace_id="format:0")
    assert len(result["blanks"]) == 5
    assert "index_sequence" in messages[1][-1]["content"]
    assert [c.kwargs["success"] for c in trace.call_args_list] == [False, True]
    assert all(c.kwargs["cost"] > 0 and c.kwargs["usage_reported"] for c in trace.call_args_list)
    assert trace.call_args.kwargs["input_data"]["content_validation"]["warnings"]


def test_bad_answer_retry_exhaustion_is_closed(monkeypatch):
    bad = payload()
    bad["blanks"][0]["answer"] = "so"
    template, messages, trace = setup_engine(monkeypatch, [bad, bad])
    with pytest.raises(StructuredOutputError):
        generate_structured(template, {}, None, trace_id="format:0")
    assert len(messages) == 2 and all(not c.kwargs["success"] for c in trace.call_args_list)


def test_coercion_cannot_hide_bad_index_type(monkeypatch):
    bad = payload()
    bad["blanks"][0]["index"] = "1"
    template, messages, _ = setup_engine(monkeypatch, [bad, payload()])
    generate_structured(template, {}, None)
    assert "index" in messages[1][-1]["content"]


def seed(db, bad=False):
    data = template_data()
    t = QuestionTemplate(**{k: v for k, v in data.items() if k != "status"})
    db.add(t)
    db.flush()
    task = GenerationTask(template_id=t.type_id, params={}, quantity=1, status="running")
    db.add(task)
    db.commit()
    draft = payload()
    if bad:
        draft["blanks"][1]["index"] = 1
    return {
        "task_id": task.id,
        "trace_id": f"{task.id}:0",
        "params": {"template_id": "cloze", "quantity": 1},
        "draft": draft,
        "qc_score": 99,
        "dimension_scores": {},
        "max_revise": 1,
        "revise_count": 0,
    }


def test_graph_invalid_checkpoint_has_feedback_and_finite_budget(db):
    state = seed(db, bad=True)
    state.update(wf.validate_node(state, db))
    assert state["status"] == "invalid" and wf.after_validate(state) == "revise"
    state.update(wf.revise_node(state, db))
    assert state["params"]["revise_context"]["validation_errors"]
    assert wf.after_validate(state) == "reject"
    wf.reject_node(state, db)
    item = db.query(ContentItem).one()
    assert item.failure_code == "deterministic_validation_failed"
    assert not item.validation_report["valid"]


@pytest.mark.parametrize("node", [wf.qc_node, wf.store_node, wf.submit_review_node])
def test_bad_checkpoint_cannot_qc_or_store_even_with_high_score(db, node):
    state = seed(db, bad=True)
    with pytest.raises(ContentValidationError):
        node(state, db)
    assert db.query(ContentItem).count() == 0


def test_store_diagnostics_are_separate_from_payload_and_provenance(db):
    state = seed(db)
    wf.store_node(state, db)
    item = db.query(ContentItem).one()
    assert item.payload == state["draft"]
    assert item.provenance is None
    assert item.validation_report["warnings"][0]["code"] == "answer_position_bias"


@pytest.mark.parametrize("status", ["pending_qc", "passed"])
def test_legacy_bad_payload_cannot_be_approved_or_published(db, status):
    state = seed(db, bad=True)
    item = ContentItem(
        task_id=state["task_id"], template_id="cloze", payload=state["draft"], status=status
    )
    db.add(item)
    db.commit()
    user = SimpleNamespace(id="admin", role="admin", tenant_id=None)
    with pytest.raises(ContentStateConflictError, match="确定性"):
        if status == "passed":
            ContentService(db).publish_content(item.id, user)
        else:
            QualityService(db).review_content(item.id, QualityReviewRequest(**{"pass": True}), user)
    assert item.status == status and db.query(QualityRecord).count() == 0


def test_valid_biased_item_still_needs_explicit_human_review(db):
    state = seed(db)
    wf.store_node(state, db)
    item = db.query(ContentItem).one()
    assert item.status == "pending_qc"
    user = SimpleNamespace(id="admin", role="admin", tenant_id=None)
    record = QualityService(db).review_content(
        item.id, QualityReviewRequest(**{"pass": True}), user
    )
    assert record.source == "manual_review"
    ContentService(db).publish_content(item.id, user)
    assert item.status == "published" and item.validation_report["warnings"]


def test_legacy_bad_item_may_be_rejected_and_diagnosed_without_mutation(db):
    state = seed(db, bad=True)
    item = ContentItem(
        task_id=state["task_id"], template_id="cloze", payload=state["draft"], status="pending_qc"
    )
    db.add(item)
    db.commit()
    user = SimpleNamespace(id="admin", role="admin", tenant_id=None)
    service = ContentService(db)
    dto = service.content_out(service.get_content(item.id, user))
    assert not dto.validation_report["valid"]
    assert item.validation_report is None and item.qc_score is None
    listed = service.list_contents(user)
    assert not listed.items[0].validation_report["valid"]
    QualityService(db).review_content(
        item.id, QualityReviewRequest(**{"pass": False, "reason": "bad format"}), user
    )
    assert item.status == "rejected"


def test_missing_template_fail_closed(db):
    from app.services.content_guard import require_valid_content

    item = ContentItem(template_id="missing", payload={})
    with pytest.raises(ContentStateConflictError, match="模板不存在"):
        require_valid_content(db, item)


def test_prompt_quantity_not_blank_count():
    prompt = (ROOT / "prompts/cloze-system.st").read_text(encoding="utf-8")
    assert "空格数与数量参数一致" not in prompt
    assert "quantity 是篇数，不是空格数" in prompt


@pytest.mark.parametrize(
    "case",
    json.loads(
        (ROOT / "tests/fixtures/opt075_quality_counterexamples.json").read_text(encoding="utf-8")
    )["cases"],
    ids=lambda case: case["id"],
)
def test_versioned_real_failure_shapes_and_unadjudicated_semantic_cases(case):
    report = inspect(case["payload"])
    assert report.valid == (case["expected"] == "format_pass_semantic_review")
    assert report.semantic_verification == "requires_human_review"
    assert case["human_adjudication"] == "pending"


def test_older_completed_qc_cache_cannot_bypass_new_rules(db, monkeypatch):
    from app.models import QualityEvaluation

    state = seed(db, bad=True)
    db.add(
        QualityEvaluation(
            task_id=state["task_id"],
            template_id="cloze",
            thread_id=state["trace_id"],
            revise_count=0,
            status="completed",
            score=99,
            dimension_scores={},
            config_snapshot={},
            threshold=70,
        )
    )
    db.commit()
    monkeypatch.setattr(
        wf,
        "run_quality_check",
        lambda *args, **kwargs: pytest.fail("cached bad draft must not reach Judge"),
    )
    with pytest.raises(ContentValidationError):
        wf.qc_node(state, db)


@pytest.mark.parametrize("bad", [False, True])
def test_direct_checkpoint_human_approval_revalidates(db, monkeypatch, bad):
    state = seed(db, bad=bad)
    item = ContentItem(
        task_id=state["task_id"],
        template_id="cloze",
        payload=state["draft"],
        status="awaiting_review",
        thread_id=state["trace_id"],
    )
    db.add(item)
    db.commit()
    state["content_id"] = item.id
    monkeypatch.setattr(
        wf, "interrupt", lambda *args: {"approved": True, "score": 100, "reviewer_id": "human"}
    )
    if bad:
        with pytest.raises(ContentStateConflictError, match="确定性"):
            wf.human_review_node(state, db)
        assert item.status == "awaiting_review" and db.query(QualityRecord).count() == 0
    else:
        result = wf.human_review_node(state, db)
        assert result["status"] == "review_passed"
        assert item.validation_report["warnings"]
