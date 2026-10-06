"""Real PostgreSQL snapshot quota/permissions/attempts and eligible example injection."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.engine.trace import record_trace
from app.engine.trace_snapshots import (
    expire_snapshots,
    prepare_snapshot,
    read_snapshot,
    store_snapshot,
)
from app.models import (
    ConfigAuditEvent,
    ContentItem,
    GenerationTask,
    QualityRecord,
    QuestionTemplate,
    TraceLog,
    TraceSnapshot,
)
from app.sample_pool import pool_item
from app.workflow import graph as wf

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "FEWSHOT_ENABLED", True)


def test_full_attempt_snapshots_and_summary_redaction_true_pg(pg_app_sessions, monkeypatch):
    from app.engine.structured_output import generate_structured

    key = "snapshot-" + uuid.uuid4().hex
    t = SimpleNamespace(
        type_id="example",
        version=1,
        run_config={"max_retry": 2},
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
        output_schema={
            "type": "object",
            "required": ["options"],
            "properties": {
                "options": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
            },
        },
    )
    answers = [{"options": ["a", "a"]}, {"options": ["a", "b"]}]
    monkeypatch.setattr("app.engine.structured_output._get_openai_client", lambda: None)
    monkeypatch.setattr(
        "app.engine.structured_output._call_openai_json",
        lambda *args: (
            json.dumps(answers.pop(0)),
            2,
            SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ),
    )
    generate_structured(
        t, {"rag_context": "教学内容" * 3000 + " Bearer private-token"}, None, trace_id=key
    )
    with pg_app_sessions() as db:
        rows = db.query(TraceLog).filter(TraceLog.trace_id == key).order_by(TraceLog.attempt).all()
        assert len(rows) == 2 and all(r.snapshot_status == "available" for r in rows)
        for r in rows:
            blob, data = read_snapshot(db, r.id)
            assert "private-token" not in str(data) and len(data["messages"][1]["content"]) > 2000
            assert "private-token" not in blob.ciphertext
            assert r.cost > 0
        assert len(read_snapshot(db, rows[1].id)[1]["messages"]) > len(
            read_snapshot(db, rows[0].id)[1]["messages"]
        )
        assert "[truncated]" in str(rows[0].input_data)


def test_parallel_snapshot_quota_is_sql_serialized(pg_engine, monkeypatch):
    factory = sessionmaker(bind=pg_engine)
    prefix = "quota-" + uuid.uuid4().hex
    # Large preexisting snapshots must not defeat the test; set quota to current bytes + one new envelope.
    with factory() as db:
        from sqlalchemy import func

        used = int(db.query(func.coalesce(func.sum(TraceSnapshot.stored_bytes), 0)).scalar())
    prepared = prepare_snapshot({"messages": [], "response_text": "test"})
    monkeypatch.setattr(settings, "TRACE_SNAPSHOT_TOTAL_BYTES", used + len(prepared.ciphertext))

    def create(index):
        with factory() as db:
            row = TraceLog(trace_id=prefix + str(index), stage="generate", cost=0.1)
            db.add(row)
            db.flush()
            store_snapshot(db, row, prepared)
            db.commit()
            return row.snapshot_status

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            states = list(pool.map(create, range(5)))
        assert states.count("available") == 1 and states.count("budget_exceeded") == 4
        with factory() as db:
            assert db.query(TraceLog).filter(TraceLog.trace_id.like(prefix + "%")).count() == 5
    finally:
        with factory() as db:
            db.query(TraceLog).filter(TraceLog.trace_id.like(prefix + "%")).delete(
                synchronize_session=False
            )
            db.commit()


def test_snapshot_api_access_audit_and_expiry_true_pg(client, auth_headers, pg_app_sessions):
    key = "access-" + uuid.uuid4().hex
    record_trace(
        trace_id=key,
        model="test",
        cost=0,
        latency_ms=0,
        stage="generate",
        snapshot_data={"messages": [], "response_text": "private-only-snapshot"},
    )
    with pg_app_sessions() as db:
        row_id = db.query(TraceLog).filter(TraceLog.trace_id == key).one().id
    result = client.get(f"/api/traces/records/{row_id}/snapshot", headers=auth_headers)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    with pg_app_sessions() as db:
        event = (
            db.query(ConfigAuditEvent)
            .filter(
                ConfigAuditEvent.entity_type == "trace_snapshot_access",
                ConfigAuditEvent.entity_id == row_id,
            )
            .one()
        )
        assert "private-only-snapshot" not in str(event.after_snapshot)
        blob = db.get(TraceSnapshot, row_id)
        blob.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert (
        client.get(f"/api/traces/records/{row_id}/snapshot", headers=auth_headers).status_code
        == 409
    )
    with pg_app_sessions() as db:
        assert expire_snapshots(db) == 1
        db.commit()
        assert db.get(TraceLog, row_id).snapshot_status == "expired"
        assert db.get(TraceSnapshot, row_id) is None


def test_fewshot_injected_through_real_graph_and_source_approval_revocation(
    pg_session, monkeypatch
):
    key = "fewshot-" + uuid.uuid4().hex
    t = QuestionTemplate(
        type_id=key,
        name="Fixture",
        version=1,
        input_schema={},
        output_schema={
            "type": "object",
            "required": ["stem"],
            "properties": {"stem": {"type": "string"}},
        },
        quality_rules=[],
        gen_prompt={},
        run_config={"rag": {"mode": "off"}},
    )
    pg_session.add(t)
    pg_session.flush()
    source = GenerationTask(
        template_id=key, params={"knowledge_point": "noun"}, quantity=1, status="succeeded"
    )
    target = GenerationTask(
        template_id=key, params={"knowledge_point": "noun"}, quantity=1, status="running"
    )
    pg_session.add_all([source, target])
    pg_session.flush()
    item = ContentItem(
        task_id=source.id,
        template_id=key,
        payload={"stem": "human-approved whole example"},
        status="passed",
    )
    pg_session.add(item)
    pg_session.flush()
    pg_session.add(
        QualityRecord(
            item_id=item.id,
            score=100,
            source="manual_review",
            reviewer="human",
            dimension_scores={},
        )
    )
    pg_session.commit()
    sample = pool_item(pg_session, item, purpose="fewshot")
    captured = {}
    monkeypatch.setattr(
        wf,
        "generate_with_fallback",
        lambda template, params, *args, **kw: captured.update(params) or {"stem": "new"},
    )
    state = {
        "task_id": target.id,
        "trace_id": target.id + ":0",
        "params": {
            "template_id": key,
            "knowledge_point": "noun",
            "fewshot_context": "forged input",
        },
    }
    result = wf.generate_node(state, pg_session)
    assert (
        "human-approved whole example" in captured["fewshot_context"]
        and "forged input" not in captured["fewshot_context"]
    )
    assert result["fewshot_report"]["selected"][0]["sample_id"] == sample.id
    item.status = "rejected"
    pg_session.commit()
    captured.clear()
    wf.generate_node(state, pg_session)
    assert "fewshot_context" not in captured
    assert (
        pg_session.query(ContentItem).count() >= 1
    )  # No generated candidate was auto-approved/published.
