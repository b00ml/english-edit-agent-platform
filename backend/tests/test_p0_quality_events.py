from types import SimpleNamespace

import pytest

from app.engine.quality_metrics import quality_pipeline_stats
from app.errors import QualityCheckError
from app.models import (
    ContentItem,
    GenerationTask,
    QualityEvaluation,
    QualityRecord,
    QuestionTemplate,
)
from app.workflow import graph as wf


def seed(db):
    template = QuestionTemplate(
        type_id="example",
        name="Example",
        version=1,
        input_schema={},
        output_schema={},
        quality_rules=[{"id": "a", "weight": 1}],
        gen_prompt={},
        run_config={"quality_threshold": 70},
    )
    db.add(template)
    db.flush()
    task = GenerationTask(template_id="example", params={}, quantity=1, status="running")
    db.add(task)
    db.commit()
    return {
        "task_id": task.id,
        "params": {"template_id": "example"},
        "trace_id": f"{task.id}:0",
        "draft": {"stem": "x"},
        "revise_count": 0,
    }


def test_quality_events_include_revise_reject_and_are_idempotent(db, monkeypatch):
    state = seed(db)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, None))
    calls = []

    def check(*args, **kwargs):
        calls.append(1)
        return 60.0, {"a": 60.0}

    monkeypatch.setattr(wf, "run_quality_check", check)
    state.update(wf.qc_node(state, db))
    wf.qc_node(state, db)
    assert len(calls) == 1
    state["revise_count"] = 1
    state.update(wf.qc_node(state, db))
    wf.reject_node(state, db)
    assert db.query(QualityEvaluation).count() == 2
    assert db.query(QualityRecord).count() == 1
    assert db.query(ContentItem).one().status == "rejected"
    stats = quality_pipeline_stats(db.query(QualityEvaluation).all())
    assert stats == {
        "threads": 1,
        "evaluations": 2,
        "first_pass_rate": 0,
        "final_pass_rate": 0,
        "quality_failure_rate": 0,
        "first_evaluated_threads": 1,
        "finalized_threads": 1,
    }


def test_failed_judge_has_durable_evaluation(db, monkeypatch):
    state = seed(db)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, None))

    def fail(*args, **kwargs):
        raise QualityCheckError("missing dimension")

    monkeypatch.setattr(wf, "run_quality_check", fail)
    with pytest.raises(QualityCheckError):
        wf.qc_node(state, db)
    event = db.query(QualityEvaluation).one()
    assert event.status == "failed" and event.score is None
    assert "missing dimension" in event.failure_reason
    assert quality_pipeline_stats([event])["quality_failure_rate"] == 1


def test_quality_metrics_use_each_event_threshold_and_unique_threads():
    def event(thread, revision, score, threshold):
        return SimpleNamespace(
            thread_id=thread,
            revise_count=revision,
            score=score,
            threshold=threshold,
            status="completed",
            is_final=True,
        )

    stats = quality_pipeline_stats(
        [
            event("a", 0, 75, 80),
            event("a", 1, 85, 80),
            event("b", 0, 65, 60),
            event("c", 0, 60, 70),
        ]
    )
    assert stats["first_pass_rate"] == pytest.approx(1 / 3)
    assert stats["final_pass_rate"] == pytest.approx(2 / 3)
    assert stats["threads"] == 3 and stats["evaluations"] == 4
    assert quality_pipeline_stats([])["final_pass_rate"] is None


def test_inflight_and_historical_partial_runs_do_not_pollute_final_or_first_denominators():
    ongoing = SimpleNamespace(
        thread_id="a", revise_count=0, score=60, threshold=70, status="completed", is_final=False
    )
    partial = SimpleNamespace(
        thread_id="b", revise_count=2, score=90, threshold=70, status="completed", is_final=True
    )
    stats = quality_pipeline_stats([ongoing, partial])
    assert stats["first_evaluated_threads"] == 1
    assert stats["finalized_threads"] == 1
    assert stats["first_pass_rate"] == 0
    assert stats["final_pass_rate"] == 1
