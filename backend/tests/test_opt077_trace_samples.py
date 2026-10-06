"""No cloud calls: private bounded snapshots, safe examples, configured A/B gates."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import settings
from app.engine import trace_snapshots as snapshots
from app.engine.fewshot import select_fewshot
from app.engine.model_comparison import ComparisonCase, compare_models, comparison_plan
from app.engine.structured_output import generate_structured
from app.errors import ModelRoutingError, TraceSnapshotError
from app.models import (
    ConfigAuditEvent,
    ContentItem,
    GenerationTask,
    ModelProfile,
    QualityRecord,
    QuestionTemplate,
    TraceLog,
    TraceSnapshot,
    User,
)
from app.prompt_loader import build_user_prompt
from app.sample_pool import pool_item
from app.security import create_access_token
from app.versioning import hash_value


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "FEWSHOT_ENABLED", True)


def store(db, data, **kw):
    row = TraceLog(
        trace_id="case:0", stage="generate", cost=1, input_data={"short": "summary"}, **kw
    )
    db.add(row)
    db.flush()
    snapshots.store_snapshot(db, row, snapshots.prepare_snapshot(data))
    db.commit()
    return row


def seed(db, tenant=None):
    t = QuestionTemplate(
        type_id="fixture",
        name="Fixture",
        version=3,
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "required": ["stem"],
            "properties": {"stem": {"type": "string", "minLength": 1}},
        },
        quality_rules=[{"id": "correct", "weight": 1}],
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
        run_config={},
    )
    db.add(t)
    db.flush()
    task = GenerationTask(
        template_id=t.type_id,
        params={"knowledge_point": "noun", "difficulty": "中"},
        quantity=1,
        status="succeeded",
        tenant_id=tenant,
    )
    db.add(task)
    db.flush()
    item = ContentItem(
        task_id=task.id,
        template_id=t.type_id,
        payload={"stem": "Human approved example"},
        status="passed",
        tenant_id=tenant,
    )
    db.add(item)
    db.flush()
    review = QualityRecord(
        item_id=item.id,
        score=100,
        source="manual_review",
        reviewer="real-reviewer",
        dimension_scores={},
        tenant_id=tenant,
    )
    db.add(review)
    db.commit()
    sample = pool_item(db, item, purpose="fewshot")
    return t, task, item, review, sample


def test_complete_redacted_encrypted_snapshot_exceeds_summary_length(db):
    data = {
        "messages": [
            {"role": "user", "content": "知识" * 4000 + " Authorization: Bearer private-credential"}
        ],
        "response_text": "reply" * 600,
        "api_key": "private-key",
    }
    original = copy.deepcopy(data)
    row = store(db, data)
    raw = db.get(TraceSnapshot, row.id)
    assert "知识" not in raw.ciphertext and "private-credential" not in raw.ciphertext
    snap, restored = snapshots.read_snapshot(db, row.id)
    assert len(restored["messages"][0]["content"]) > 2000
    assert restored["api_key"] == "[REDACTED]" and "private-credential" not in str(restored)
    assert snap.replay_level == "redacted_request_response" and data == original
    assert row.snapshot_status == "available"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("disabled", "disabled"),
        ("missing", "not_requested"),
        ("key", "key_unavailable"),
        ("oversize", "oversize"),
        ("serialize", "serialization_error"),
    ],
)
def test_snapshot_unavailable_reasons_preserve_summary(db, monkeypatch, kind, expected):
    data = {"messages": [], "response_text": "x"}
    if kind == "disabled":
        monkeypatch.setattr(settings, "TRACE_SNAPSHOT_ENABLED", False)
    if kind == "missing":
        data = None
    if kind == "key":
        monkeypatch.setattr(settings, "TRACE_SNAPSHOT_SECRET_KEY", "")
    if kind == "oversize":
        monkeypatch.setattr(settings, "TRACE_SNAPSHOT_MAX_BYTES", 100)
        data["response_text"] = "x" * 500
    if kind == "serialize":
        data["number"] = float("nan")
    row = store(db, data)
    assert row.snapshot_status == expected and row.cost == 1
    assert db.query(TraceSnapshot).count() == 0
    with pytest.raises(TraceSnapshotError):
        snapshots.read_snapshot(db, row.id)


def test_quota_and_expiration_are_bounded_and_costs_survive(db, monkeypatch):
    first = store(db, {"response_text": "one"})
    stored = db.get(TraceSnapshot, first.id)
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_TOTAL_BYTES", stored.stored_bytes)
    blocked = store(db, {"response_text": "two"})
    assert blocked.snapshot_status == "budget_exceeded" and blocked.cost == 1
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with pytest.raises(TraceSnapshotError, match="保留期"):
        snapshots.read_snapshot(db, first.id)
    assert snapshots.expire_snapshots(db) == 1
    db.commit()
    db.refresh(first)
    assert first.snapshot_status == "expired" and db.query(TraceLog).count() == 2


def test_wrong_key_hash_corruption_and_request_only(db, monkeypatch):
    row = store(db, {"messages": [], "response_text": None})
    assert snapshots.read_snapshot(db, row.id)[0].replay_level == "request_only"
    blob = db.get(TraceSnapshot, row.id)
    blob.content_hash = "bad"
    db.commit()
    with pytest.raises(TraceSnapshotError, match="完整性"):
        snapshots.read_snapshot(db, row.id)
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(TraceSnapshotError, match="解密"):
        snapshots.read_snapshot(db, row.id)


def test_snapshot_access_scoped_and_audited_without_plaintext(db):
    from app.database import get_db
    from app.main import create_app

    row = store(db, {"response_text": "private teaching body"}, tenant_id="owner")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    for role, tenant, status in [
        ("researcher", "other", 403),
        ("viewer", "owner", 403),
        ("researcher", "owner", 200),
    ]:
        user = User(
            username=role + tenant,
            password_hash="unused",
            role=role,
            status="active",
            tenant_id=tenant,
        )
        db.add(user)
        db.commit()
        resp = client.get(
            f"/api/traces/records/{row.id}/snapshot",
            headers={"Authorization": "Bearer " + create_access_token(user)},
        )
        assert resp.status_code == status, resp.text
        if status == 200:
            assert resp.headers["cache-control"] == "no-store"
    audit = (
        db.query(ConfigAuditEvent)
        .filter(ConfigAuditEvent.entity_type == "trace_snapshot_access")
        .one()
    )
    assert "private teaching body" not in str(audit.after_snapshot)


def test_generation_retries_capture_exact_messages_and_raw_replies(monkeypatch):
    from test_p0_jsonschema_retry import setup_generation

    template, messages, record = setup_generation(
        monkeypatch, [{"options": ["A", "A"]}, {"options": ["A", "B"]}]
    )
    generate_structured(template, {}, None)
    for index, call in enumerate(record.call_args_list):
        snapshot = call.kwargs["snapshot_data"]
        # Mocks retain shared lists by reference; actual prepare encrypts immediately. Compare frozen sent prefixes.
        assert snapshot["messages"][: len(messages[index])] == messages[index]
        assert json.loads(snapshot["response_text"])["options"] == (
            ["A", "A"] if index == 0 else ["A", "B"]
        )


def test_fewshot_selection_scope_whole_payload_and_provenance(db):
    t, task, item, review, sample = seed(db)
    params = {"knowledge_point": "noun", "tenant_id": None}
    result = select_fewshot(db, t, params)
    assert len(result["selected"]) == 1 and result["selected"][0]["review_record_id"] == review.id
    assert result["selected"][0]["payload_hash"] == hash_value(item.payload)
    assert result["context"] and result["chars"] == len(result["context"])
    assert select_fewshot(db, t, {**params, "tenant_id": "other"})["selected"] == []
    assert (
        select_fewshot(db, t, params, task_id=task.id)["skipped"]["same_task_or_missing_source"]
        == 1
    )
    assert select_fewshot(db, t, {"knowledge_point": "verb"})["selected"] == []


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("pending", "source_not_eligible"),
        ("manual", "human_review_missing"),
        ("score", "human_review_missing"),
        ("payload", "stale_payload"),
        ("source", "reference_unverified"),
        ("schema", "current_schema_invalid"),
        ("budget", "whole_example_exceeds_budget"),
    ],
)
def test_fewshot_never_uses_unverified_stale_or_partial_examples(db, monkeypatch, kind, reason):
    t, task, item, review, sample = seed(db)
    if kind == "pending":
        item.status = "pending_qc"
    if kind == "manual":
        db.delete(review)
    if kind == "score":
        review.score = 0
    if kind == "payload":
        sample.payload = {"stem": "modified"}
    if kind == "source":
        item.provenance = {"require_review": True, "reference_review": {"verified": False}}
    if kind == "schema":
        t.output_schema = {"type": "object", "required": ["answer"]}
    if kind == "budget":
        monkeypatch.setattr(settings, "FEWSHOT_MAX_CHARS", 1)
    db.commit()
    report = select_fewshot(db, t, {"knowledge_point": "noun"})
    assert not report["selected"] and report["skipped"][reason] == 1


def test_only_explicit_fewshot_purpose_and_disable_switch(db, monkeypatch):
    t, task, item, review, sample = seed(db)
    sample.purpose = "sft"
    db.commit()
    assert not select_fewshot(db, t, {"knowledge_point": "noun"})["selected"]
    monkeypatch.setattr(settings, "FEWSHOT_ENABLED", False)
    assert not select_fewshot(db, t, {"knowledge_point": "noun"})["enabled"]


def test_prompt_examples_are_not_rag_or_judge_constraints(db):
    t, *_ = seed(db)
    params = {
        "knowledge_point": "noun",
        "fewshot_context": "trusted example",
        "rag_context": "source evidence",
    }
    prompt = build_user_prompt(t, params)
    assert (
        "trusted example" in prompt
        and "不是本次题目的参考来源" in prompt
        and "source evidence" in prompt
    )
    from app.engine.quality import _build_judge_prompt

    _, judge = _build_judge_prompt(t, {"stem": "new"}, params)
    assert "trusted example" not in judge and "source evidence" not in judge


def test_same_effective_model_refuses_paid_comparison(db, monkeypatch):
    seed(db)
    db.add_all(
        [
            ModelProfile(name=n, provider="env", model_name="same", is_default=False)
            for n in ("a", "b")
        ]
    )
    db.commit()
    plan = comparison_plan(db, "a", "b")
    assert not plan["ready"] and plan["same_effective_model"]
    monkeypatch.setattr(
        "app.engine.model_comparison.build_rag_context",
        lambda *a, **kw: pytest.fail("no embedding before configuration gate"),
    )
    with pytest.raises(ModelRoutingError):
        compare_models(
            db, [ComparisonCase(case_id="one", template_id="fixture", params={})], "a", "b"
        )
    with pytest.raises(ModelRoutingError):
        comparison_plan(db, "missing", "b")


def test_model_comparison_frozen_context_counterbalanced_no_promotion(db, monkeypatch):
    t, *_ = seed(db)
    db.add_all(
        [ModelProfile(name=n, provider="env", model_name=n, is_default=False) for n in ("a", "b")]
    )
    db.commit()
    contexts = []
    called = []
    monkeypatch.setattr(
        "app.engine.model_comparison.build_rag_context",
        lambda *a, **kw: contexts.append(kw) or "same-source",
    )
    monkeypatch.setattr(
        "app.engine.model_comparison.generate_structured",
        lambda template, params, profile, **kw: called.append((profile.name, copy.deepcopy(params)))
        or {"stem": "new"},
    )
    before = db.query(ContentItem).count()
    report = compare_models(
        db,
        [
            ComparisonCase(
                case_id=str(i), template_id="fixture", params={"knowledge_point": "noun"}
            )
            for i in range(2)
        ],
        "a",
        "b",
    )
    assert [c[0] for c in called] == ["a", "b", "b", "a"] and len(contexts) == 2
    assert report["human_quality_verdict"] is None and report["no_content_published"]
    assert report["results"][0]["input_hash"] == report["results"][1]["input_hash"]
    assert db.query(ContentItem).count() == before
    with pytest.raises(ValueError):
        compare_models(db, [], "a", "b")


def test_exact_fewshot_copy_rejected_and_feedback_retry(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    t = SimpleNamespace(
        type_id="fixture",
        version=1,
        run_config={"max_retry": 2},
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
        output_schema={
            "type": "object",
            "properties": {"stem": {"type": "string"}},
            "required": ["stem"],
        },
    )
    answers = [{"stem": "approved sample"}, {"stem": "new question"}]
    sent = []

    def call(_client, _model, messages):
        sent.append(copy.deepcopy(messages))
        return json.dumps(answers.pop(0)), 2, SimpleNamespace(prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr("app.engine.structured_output._get_openai_client", lambda: None)
    monkeypatch.setattr("app.engine.structured_output._call_openai_json", call)
    recorder = Mock()
    monkeypatch.setattr("app.engine.structured_output.record_trace", recorder)
    result = generate_structured(
        t,
        {
            "fewshot_provenance": {
                "selected": [{"payload_hash": hash_value({"stem": "approved sample"})}]
            }
        },
        None,
    )
    assert result["stem"] == "new question"
    assert "fewshot_exact_copy" in sent[1][-1]["content"]
    assert [c.kwargs["success"] for c in recorder.call_args_list] == [False, True]


def test_raw_json_response_secret_is_redacted(db):
    raw = '{"api_key": "private key with spaces", "llm_api_key": "another private key", "stem":"keep body"}'
    row = store(db, {"messages": [], "response_text": raw})
    _, data = snapshots.read_snapshot(db, row.id)
    assert "private key" not in data["response_text"]
    parsed = json.loads(data["response_text"])
    assert parsed["api_key"] == "[REDACTED]" and parsed["llm_api_key"] == "[REDACTED]"
    assert parsed["stem"] == "keep body"


def test_model_comparison_keeps_failed_candidate_and_bounded_ids(db, monkeypatch):
    from fastapi import HTTPException

    t, *_ = seed(db)
    db.add_all([ModelProfile(name=n, provider="env", model_name=n) for n in ("a", "b")])
    db.commit()
    monkeypatch.setattr("app.engine.model_comparison.build_rag_context", lambda *args, **kw: "")
    calls = []

    def generate(template, params, profile, **kw):
        calls.append(kw["trace_id"])
        if profile.name == "a":
            raise HTTPException(401, "private upstream error")
        return {"stem": "new"}

    monkeypatch.setattr("app.engine.model_comparison.generate_structured", generate)
    report = compare_models(
        db,
        [
            ComparisonCase(
                case_id="长" * 128, template_id="fixture", params={"knowledge_point": "noun"}
            )
        ],
        "a",
        "b",
    )
    assert [r["status"] for r in report["results"]] == ["failed", "generated"]
    assert "private upstream error" not in str(report)
    assert all(len(value) < 128 for value in calls)


def test_fewshot_foreign_review_scope_cannot_attest_local_example(db):
    t, task, item, review, sample = seed(db)
    review.tenant_id = "other"
    db.commit()
    report = select_fewshot(db, t, {"knowledge_point": "noun"})
    assert not report["selected"] and report["skipped"]["human_review_missing"] == 1
