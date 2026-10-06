import json
from unittest.mock import Mock

import pytest

from app.config import settings
from app.engine import trace as traces
from app.engine.observability import TraceLogSink
from app.engine.trace_recovery import replay_spool, reset_diagnostics, trace_diagnostics
from app.errors import TracePersistenceError
from app.models import TraceLog


@pytest.fixture(autouse=True)
def clean_diagnostics(monkeypatch):
    reset_diagnostics()
    monkeypatch.setattr(settings, "TRACE_REQUIRE_DURABILITY", False)
    monkeypatch.setattr(settings, "TRACE_FAILURE_SPOOL_DIR", "")
    yield
    reset_diagnostics()


class BrokenSink:
    name = "db"

    def emit(self, trace):
        raise RuntimeError("password=secret-should-not-appear")


def record():
    traces.record_trace(
        trace_id="t",
        model="test",
        cost=0.5,
        latency_ms=2,
        input_data={"api_key": "secret"},
        output_data={"stem": "test"},
        usage_reported=True,
        prompt_cost=0.3,
        completion_cost=0.2,
    )


def test_failed_db_write_spools_sanitized_record_and_replays_idempotently(
    db, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "TRACE_FAILURE_SPOOL_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "TRACE_REQUIRE_DURABILITY", True)
    monkeypatch.setattr(traces, "_get_sinks", lambda: [BrokenSink()])
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    record()
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and "secret" not in files[0].read_text(encoding="utf-8")
    assert trace_diagnostics()["status"] == "degraded"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    sink = TraceLogSink()
    sink.emit(payload)
    assert replay_spool(sink.emit) == {"replayed": 1, "failed": 0}
    assert db.query(TraceLog).count() == 1 and not list(tmp_path.glob("*.json"))
    row = db.query(TraceLog).one()
    assert row.prompt_cost == 0.3 and row.completion_cost == 0.2 and row.usage_reported


def test_strict_durability_failure_is_visible_not_silently_successful(monkeypatch):
    monkeypatch.setattr(settings, "TRACE_REQUIRE_DURABILITY", True)
    monkeypatch.setattr(traces, "_get_sinks", lambda: [BrokenSink()])
    with pytest.raises(TracePersistenceError):
        record()
    assert trace_diagnostics()["status"] == "unavailable"
    assert trace_diagnostics()["sinks"]["db"]["failures"] == 1


def test_replay_failure_keeps_durable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TRACE_FAILURE_SPOOL_DIR", str(tmp_path))
    monkeypatch.setattr(traces, "_get_sinks", lambda: [BrokenSink()])
    record()
    assert replay_spool(BrokenSink().emit) == {"replayed": 0, "failed": 1}
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_recovered_sink_does_not_hide_previously_unrecoverable_trace(monkeypatch):
    sink = Mock(name="healthy")
    sink.name = "db"
    monkeypatch.setattr(traces, "_get_sinks", lambda: [BrokenSink()])
    record()
    monkeypatch.setattr(traces, "_get_sinks", lambda: [sink])
    record()
    assert trace_diagnostics()["status"] == "unavailable"
    assert trace_diagnostics()["sinks"]["db"]["attempts"] == 2


def test_budget_guard_counts_durable_but_unreplayed_task_cost(db, monkeypatch, tmp_path):
    from app.engine.router import _task_cost
    from app.engine.trace_recovery import spool_trace

    monkeypatch.setattr(settings, "TRACE_FAILURE_SPOOL_DIR", str(tmp_path))
    spool_trace({"id": "pending-one", "task_id": "task-one", "cost": 0.75})
    assert _task_cost(db, "task-one") == 0.75
    assert _task_cost(db, "other-task") == 0


def test_unreadable_cost_spool_does_not_silently_reset_budget(db, monkeypatch, tmp_path):
    from app.engine.router import _task_cost

    monkeypatch.setattr(settings, "TRACE_FAILURE_SPOOL_DIR", str(tmp_path))
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    with pytest.raises(TracePersistenceError):
        _task_cost(db, "task-one")


def test_health_endpoint_blocks_strict_unrecoverable_trace(monkeypatch):
    from app.health import readiness_report

    dependencies = [
        {"name": name, "status": "healthy", "latency_ms": 0, "detail": "ok"}
        for name in ["database", "alembic", "redis", "checkpointer"]
    ]
    dependencies.append(
        {"name": "trace", "status": "unavailable", "latency_ms": 0, "detail": "lost"}
    )
    monkeypatch.setattr(settings, "TRACE_REQUIRE_DURABILITY", True)
    monkeypatch.setattr(
        "app.health.dependency_report",
        lambda: {"status": "unavailable", "dependencies": dependencies},
    )
    result = readiness_report()
    assert not result["ready"] and "trace" in result["blocking"]


def test_empty_usage_object_does_not_claim_zero_tokens_are_provider_reported():
    from types import SimpleNamespace

    from app.engine.trace import usage_is_reported

    assert not usage_is_reported(SimpleNamespace(), "generate")
    assert not usage_is_reported(SimpleNamespace(prompt_tokens=True, completion_tokens=0), "qc")
    assert usage_is_reported(SimpleNamespace(prompt_tokens=0, completion_tokens=0), "generate")
    assert usage_is_reported(SimpleNamespace(prompt_tokens=10), "embedding")
