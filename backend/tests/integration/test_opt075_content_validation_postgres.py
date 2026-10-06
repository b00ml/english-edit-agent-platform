"""Real migrated PostgreSQL/API/Trace; no provider requests or shared data deletion."""

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import inspect, text

from app.engine.structured_output import generate_structured
from app.models import ContentItem, GenerationTask, QualityRecord, QuestionTemplate, TraceLog
from app.workflow import graph as wf

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def seed(session, bad=False):
    data = yaml.safe_load((ROOT / "app/templates/cloze.yaml").read_text(encoding="utf-8"))
    data["type_id"] = "guard-" + uuid.uuid4().hex
    t = QuestionTemplate(**data)
    session.add(t)
    session.flush()
    task = GenerationTask(template_id=t.type_id, params={}, quantity=1, status="running")
    session.add(task)
    session.commit()
    draft = {
        "passage": " ".join(["A ____ follows."] * 5),
        "blanks": [
            {
                "index": i + 1,
                "options": ["so", "and", "but", "or"],
                "answer": "A",
                "explanation": "Review semantics manually.",
            }
            for i in range(5)
        ],
    }
    if bad:
        draft["blanks"][1]["index"] = 1
    return t, {
        "task_id": task.id,
        "trace_id": f"{task.id}:0",
        "params": {"template_id": t.type_id},
        "draft": draft,
        "qc_score": 99,
        "dimension_scores": {},
        "revise_count": 0,
        "max_revise": 0,
    }


def test_actual_migration_and_jsonb_report_are_durable(pg_session):
    columns = {c["name"]: c for c in inspect(pg_session.get_bind()).get_columns("content_item")}
    assert str(columns["validation_report"]["type"]) == "JSONB"
    assert (
        pg_session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        == "opt079_public_merge"
    )
    _, state = seed(pg_session)
    result = wf.store_node(state, pg_session)
    pg_session.expire_all()
    item = pg_session.get(ContentItem, result["content_id"])
    assert item.payload == state["draft"]
    assert item.validation_report["valid"] and item.validation_report["warnings"]
    assert item.status == "pending_qc"


def test_true_api_reads_legacy_diagnostics_blocks_approval_not_rejection(
    client, auth_headers, pg_app_sessions
):
    with pg_app_sessions() as session:
        t, state = seed(session, bad=True)
        item = ContentItem(
            task_id=state["task_id"],
            template_id=t.type_id,
            payload=state["draft"],
            qc_score=99,
            status="pending_qc",
        )
        session.add(item)
        session.commit()
        item_id = item.id
    detail = client.get(f"/api/contents/{item_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert not detail.json()["validation_report"]["valid"]
    before = detail.json()["payload"]
    resp = client.post(f"/api/quality/{item_id}/review", headers=auth_headers, json={"pass": True})
    assert resp.status_code == 409, resp.text
    with pg_app_sessions() as session:
        assert session.get(ContentItem, item_id).status == "pending_qc"
        assert session.query(QualityRecord).filter(QualityRecord.item_id == item_id).count() == 0
    reject = client.post(
        f"/api/quality/{item_id}/review",
        headers=auth_headers,
        json={"pass": False, "reason": "invalid index"},
    )
    assert reject.status_code == 200, reject.text
    assert client.get(f"/api/contents/{item_id}", headers=auth_headers).json()["payload"] == before


def test_true_api_publication_rechecks_legacy_high_score(client, auth_headers, pg_app_sessions):
    with pg_app_sessions() as session:
        t, state = seed(session, bad=True)
        item = ContentItem(
            task_id=state["task_id"],
            template_id=t.type_id,
            payload=state["draft"],
            qc_score=99,
            status="passed",
        )
        session.add(item)
        session.commit()
        item_id = item.id
    resp = client.post(f"/api/contents/{item_id}/publish", headers=auth_headers)
    assert resp.status_code == 409, resp.text
    with pg_app_sessions() as session:
        assert session.get(ContentItem, item_id).status == "passed"
        assert session.get(ContentItem, item_id).published_at is None


def test_mock_model_attempts_record_true_pg_trace_and_cost(pg_app_sessions, monkeypatch):
    with pg_app_sessions() as session:
        t, state = seed(session)
        bad = copy.deepcopy(state["draft"])
        bad["blanks"][1]["index"] = 1
        responses = [bad, state["draft"]]
        monkeypatch.setattr("app.engine.structured_output._get_openai_client", lambda: None)
        monkeypatch.setattr(
            "app.engine.structured_output._call_openai_json",
            lambda *args: (
                json.dumps(responses.pop(0)),
                5.0,
                SimpleNamespace(prompt_tokens=20, completion_tokens=10),
            ),
        )
        result = generate_structured(
            t,
            {"quantity": 1},
            None,
            trace_id=state["trace_id"],
            task_id=state["task_id"],
            template_id=t.type_id,
        )
        session.expire_all()
        traces = (
            session.query(TraceLog)
            .filter(TraceLog.trace_id == state["trace_id"])
            .order_by(TraceLog.attempt)
            .all()
        )
        assert len(traces) == 2 and [x.success for x in traces] == [False, True]
        assert all(x.cost > 0 for x in traces)
        assert "index_sequence" in traces[0].output_data["error_summary"]
        assert result == state["draft"]


def test_full_graph_invalid_mock_draft_rejects_with_bounded_revision(pg_session, monkeypatch):
    t, state = seed(pg_session, bad=True)
    monkeypatch.setattr(
        wf,
        "_get_checkpointer",
        lambda: __import__("langgraph.checkpoint.memory", fromlist=["MemorySaver"]).MemorySaver(),
    )
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        return state["draft"]

    monkeypatch.setattr(wf, "generate_with_fallback", generate)
    monkeypatch.setattr(wf, "build_rag_context", lambda *args, **kwargs: "source context")
    monkeypatch.setattr(
        wf,
        "run_quality_check",
        lambda *args, **kwargs: pytest.fail("Invalid draft must not reach Judge"),
    )
    result = wf.build_graph(pg_session).invoke(
        state, config={"configurable": {"thread_id": state["trace_id"]}}
    )
    assert result["status"] == "rejected" and len(calls) == 1
    item = pg_session.get(ContentItem, result["content_id"])
    assert item.failure_code == "deterministic_validation_failed"
    assert not item.validation_report["valid"]
