import pytest
from test_p0_quality_events import seed

from app.config import settings
from app.errors import CheckpointerUnavailableError
from app.models import ContentItem, GenerationTaskItem, QualityRecord
from app.workflow import graph as wf


def wire(monkeypatch, score=90):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
    wf.reset_checkpointer()
    count = {"gen": 0, "qc": 0}

    def gen(*args, **kwargs):
        count["gen"] += 1
        return {"stem": "x"}

    def qc(*args, **kwargs):
        count["qc"] += 1
        return score, {"a": score}

    monkeypatch.setattr(wf, "generate_with_fallback", gen)
    monkeypatch.setattr(wf, "run_quality_check", qc)
    monkeypatch.setattr(wf, "record_lifecycle_event", lambda **kwargs: None)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(wf, "build_rag_context", lambda *args, **kwargs: "")
    return count


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_production_memory_saver_is_rejected(monkeypatch, environment):
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(settings, "ALLOW_MEMORY_CHECKPOINTER", False)
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
    wf.reset_checkpointer()
    with pytest.raises(CheckpointerUnavailableError):
        wf._get_checkpointer()
    wf.reset_checkpointer()


def test_completed_thread_replay_does_not_regenerate(db, monkeypatch):
    state = seed(db)
    count = wire(monkeypatch)
    args = (state["task_id"], "example", {}, db, state["trace_id"])
    result = wf.run_generation(*args)
    assert wf.run_generation(*args)["content_id"] == result["content_id"]
    assert count == {"gen": 1, "qc": 1}
    assert db.query(ContentItem).count() == 1
    wf.reset_checkpointer()


def test_failed_node_resumes_checkpoint_without_generating_again(db, monkeypatch):
    state = seed(db)
    count = wire(monkeypatch)
    original = wf.qc_node

    def broken(*args, **kwargs):
        raise RuntimeError("simulated crash before qc")

    monkeypatch.setattr(wf, "qc_node", broken)
    args = (state["task_id"], "example", {}, db, state["trace_id"])
    with pytest.raises(RuntimeError):
        wf.run_generation(*args)
    monkeypatch.setattr(wf, "qc_node", original)
    assert wf.run_generation(*args)["status"] == "stored"
    assert count == {"gen": 1, "qc": 1}
    wf.reset_checkpointer()


def test_committed_store_replay_is_idempotent(db, monkeypatch):
    state = seed(db)
    count = wire(monkeypatch)
    original = wf.store_node

    def crash_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash after store commit")

    monkeypatch.setattr(wf, "store_node", crash_after_commit)
    args = (state["task_id"], "example", {}, db, state["trace_id"])
    with pytest.raises(RuntimeError):
        wf.run_generation(*args)
    monkeypatch.setattr(wf, "store_node", original)
    assert wf.run_generation(*args)["status"] == "stored"
    assert count == {"gen": 1, "qc": 1}
    assert db.query(ContentItem).count() == db.query(QualityRecord).count() == 1
    wf.reset_checkpointer()


def test_gray_replay_and_review_are_idempotent(db, monkeypatch):
    from app.models import QuestionTemplate

    state = seed(db)
    template = db.query(QuestionTemplate).one()
    template.run_config = {
        "quality_threshold": 70,
        "human_review": {"enabled": True, "gray_margin": 10},
    }
    db.add(
        GenerationTaskItem(
            task_id=state["task_id"],
            thread_id=state["trace_id"],
            item_index=0,
            status="awaiting_review",
            tenant_id="default",
        )
    )
    db.commit()
    count = wire(monkeypatch, score=65)
    monkeypatch.setattr(wf, "notify_human_review", lambda *args: None)
    args = (state["task_id"], "example", {}, db, state["trace_id"])
    assert wf.run_generation(*args)["__interrupt__"]
    assert wf.run_generation(*args)["status"] == "awaiting_review"
    decision = {"approved": True, "score": 100, "reviewer_id": "human"}
    assert wf.resume_human_review(state["trace_id"], decision, db)["status"] == "review_passed"
    assert wf.resume_human_review(state["trace_id"], decision, db)["status"] == "review_passed"
    assert count == {"gen": 1, "qc": 1}
    assert db.query(QualityRecord).filter_by(source="manual_review").count() == 1
    assert db.query(GenerationTaskItem).one().status == "succeeded"
    wf.reset_checkpointer()


def test_checkpoint_initialization_failure_does_not_fallback_in_production(monkeypatch):
    import psycopg

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_MEMORY_CHECKPOINTER", False)
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "postgres")

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("simulated outage")

    monkeypatch.setattr(psycopg, "connect", refuse)
    wf.reset_checkpointer()
    with pytest.raises(CheckpointerUnavailableError):
        wf._get_checkpointer()
    wf.reset_checkpointer()


def test_terminal_quality_failure_updates_child_and_parent(db, monkeypatch):
    from app.errors import QualityCheckError
    from app.models import GenerationTask, GenerationTaskItem
    from app.worker import tasks

    state = seed(db)
    db.add(
        GenerationTaskItem(
            task_id=state["task_id"],
            item_index=0,
            thread_id=state["trace_id"],
            status="pending",
            tenant_id="default",
        )
    )
    db.commit()

    def fail(*args, **kwargs):
        raise QualityCheckError("invalid judge exhausted")

    monkeypatch.setattr(tasks, "run_generation", fail)
    result = tasks.generate_single_item.apply(args=[state["task_id"], 0]).get()
    assert result["failure_code"] == "QUALITY_CHECK_FAILED"
    assert db.query(GenerationTaskItem).one().status == "failed"
    parent = db.query(GenerationTask).one()
    assert parent.status == "failed" and parent.progress == 1


def test_review_api_retry_reuses_record_instead_of_duplication(db, monkeypatch):
    from types import SimpleNamespace

    from app.schemas import QualityReviewRequest
    from app.services.quality_service import QualityService

    state = seed(db)
    wire(monkeypatch)
    result = wf.run_generation(state["task_id"], "example", {}, db, state["trace_id"])
    request = QualityReviewRequest(**{"pass": True})
    service = QualityService(db)
    reviewer = SimpleNamespace(id="human", tenant_id=None, role="reviewer")
    first = service.review_content(result["content_id"], request, reviewer)
    second = service.review_content(result["content_id"], request, reviewer)
    assert first.id == second.id
    assert db.query(QualityRecord).filter_by(source="manual_review").count() == 1
    wf.reset_checkpointer()


def test_review_cannot_reverse_terminal_or_unpublish_content(db, monkeypatch):
    from types import SimpleNamespace

    from app.errors import ContentStateConflictError
    from app.schemas import QualityReviewRequest
    from app.services.quality_service import QualityService

    state = seed(db)
    wire(monkeypatch)
    result = wf.run_generation(state["task_id"], "example", {}, db, state["trace_id"])
    service = QualityService(db)
    reviewer = SimpleNamespace(id="human", tenant_id=None, role="reviewer")
    service.review_content(result["content_id"], QualityReviewRequest(**{"pass": True}), reviewer)
    item = db.query(ContentItem).one()
    item.status = "published"
    db.commit()
    with pytest.raises(ContentStateConflictError):
        service.review_content(
            result["content_id"],
            QualityReviewRequest(**{"pass": False, "reason": "reverse"}),
            reviewer,
        )
    assert item.status == "published"
    assert db.query(QualityRecord).filter_by(source="manual_review").count() == 1
    wf.reset_checkpointer()
