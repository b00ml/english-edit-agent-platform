"""No paid providers: batch contract, SQL dispatch leases, replay and broker-loss recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.engine.structured_output import _normalize_flat
from app.models import ContentItem, GenerationTask, GenerationTaskItem, QuestionTemplate, TaskOutbox
from app.outbox import (
    _ack,
    _claim,
    create_generation_dispatch,
    create_item_dispatch,
    relay_pending,
    replay_dead,
)
from app.prompt_loader import build_user_prompt
from app.schemas import GenerateRequest
from app.services.generation_service import GenerationService
from app.worker import tasks
from app.worker.execution_lock import item_execution_lock
from app.worker.recovery import reconcile_generation


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(tasks, "record_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(settings, "GENERATION_DELIVERY_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(settings, "GENERATION_STALE_ITEM_SECONDS", 960)
    monkeypatch.setattr(settings, "OUTBOX_RETRY_BACKOFF_SECONDS", 5)
    monkeypatch.setattr(settings, "OUTBOX_MAX_ATTEMPTS", 3)


def seed(db, quantity=3, status="pending", tenant="test"):
    template = QuestionTemplate(
        type_id="fixture",
        name="Fixture",
        version=1,
        input_schema={"type": "object", "properties": {"quantity": {"type": "integer"}}},
        output_schema={},
        quality_rules=[],
        gen_prompt={},
        run_config={},
    )
    db.add(template)
    db.flush()
    task = GenerationTask(
        template_id="fixture",
        params={"quantity": quantity},
        quantity=quantity,
        status=status,
        tenant_id=tenant,
    )
    db.add(task)
    db.commit()
    return task


def items(db, task, status="pending"):
    rows = [
        GenerationTaskItem(
            task_id=task.id,
            item_index=i,
            thread_id=f"{task.id}:{i}",
            tenant_id=task.tenant_id,
            status=status,
        )
        for i in range(task.quantity)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def old_sent(db, event):
    event.status = "sent"
    event.attempts = 1
    event.sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()


def test_service_normalizes_quantity_and_batch_participates_in_dedup(db, monkeypatch):
    seed(db)
    monkeypatch.setattr(
        "app.services.generation_service.relay_pending", lambda *a, **kw: {"scanned": 0}
    )
    from app.models import User

    db.add(
        User(
            id="user",
            username="fixture-user",
            password_hash="test-only",
            role="researcher",
            status="active",
            tenant_id="test",
        )
    )
    db.commit()
    user = SimpleNamespace(id="user", role="researcher", tenant_id="test")
    api = GenerationService(db)
    ids = []
    for batch in [2, 5]:
        result = api.create_task(
            GenerateRequest(template_id="fixture", params={"quantity": batch}, quantity=batch),
            user,
            Mock(),
        )
        task = db.get(GenerationTask, result.task_id)
        ids.append(task.id)
        assert task.quantity == batch and task.params["quantity"] == 1
    assert len(set(ids)) == 2


@pytest.mark.parametrize("n", [2, 5, 8])
def test_worker_normalizes_legacy_params_and_keeps_batch_size(db, monkeypatch, n):
    task = seed(db, n)
    items(db, task)
    captured = []

    def run(**kw):
        captured.append(kw["params"])
        return {"status": "stored"}

    monkeypatch.setattr(tasks, "run_generation", run)
    for i in range(n):
        assert tasks.generate_single_item.apply(args=[task.id, i]).get()["status"] == "succeeded"
    assert len(captured) == n and all(p["quantity"] == 1 for p in captured)
    assert task.params["quantity"] == n and task.quantity == n


@pytest.mark.parametrize("name", ["single_choice", "cloze", "reading"])
def test_singleton_prompt_unit_not_batch(name):
    prompt = build_user_prompt(
        SimpleNamespace(gen_prompt={"user": f"prompts/{name}-user.st"}), {"quantity": 1}
    )
    assert "1 道" in prompt if name == "single_choice" else "1 篇" in prompt


def test_model_multi_object_wrapper_is_not_silently_truncated():
    schema = {"type": "object", "required": ["stem", "answer"]}
    data = {"items": [{"stem": "one", "answer": "A"}, {"stem": "two", "answer": "B"}]}
    assert _normalize_flat(schema, data) is data
    assert _normalize_flat(schema, {"items": [data["items"][0]]}) == data["items"][0]


def test_partial_child_delivery_has_one_intent_per_item_and_can_finish(db, monkeypatch):
    task = seed(db)
    calls = []

    def first(name, args, **kw):
        calls.append(args[1])
        if args[1] == 1:
            raise ConnectionError("secret must not be saved")

    monkeypatch.setattr(tasks, "_item_sender", first)
    tasks.dispatch_generation_items.apply(args=[task.id]).get()
    events = db.query(TaskOutbox).filter_by(event_type="generation_item_dispatch").all()
    assert len(events) == 3 and {e.status for e in events} == {"sent", "pending"}
    retry = next(e for e in events if e.status == "pending")
    assert retry.last_error == "ConnectionError" and "secret" not in retry.last_error
    retry.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    result = relay_pending(db, lambda name, args, **kw: calls.append(args[1]))
    assert result["sent"] == 1 and sorted(calls[:3]) == [0, 1, 2] and calls[-1] == 1
    monkeypatch.setattr(
        tasks, "_item_sender", lambda *a, **kw: pytest.fail("Do not resend sent items")
    )
    tasks.dispatch_generation_items.apply(args=[task.id]).get()
    assert db.query(TaskOutbox).filter_by(event_type="generation_item_dispatch").count() == 3


def test_fenced_claim_prevents_late_ack_and_preserves_new_owner(db):
    task = seed(db, 1)
    event = create_generation_dispatch(db, task)
    db.commit()
    first = _claim(db)
    assert first and _claim(db) is None
    event = db.get(TaskOutbox, event.id)
    event.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = _claim(db)
    assert second and first[1] != second[1]
    assert not _ack(db, first[0], first[1], {"status": "sent"})
    assert _ack(db, second[0], second[1], {"status": "sent"})


def test_exhausted_dispatch_replay_has_new_cycle_without_erasing_attempts(db):
    task = seed(db, 1)
    event = create_generation_dispatch(db, task)
    event.status = "dead"
    event.attempts = 7
    event.last_error = "ConnectionError"
    db.commit()
    replay_dead(db, event.id)
    assert event.retry_base == 7 and event.attempts == 7 and event.last_error == "ConnectionError"
    result = relay_pending(db, lambda *a, **kw: None, only_event_id=event.id)
    assert result["sent"] == 1 and event.attempts == 8


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "awaiting_review"])
def test_terminal_parent_replay_or_old_message_cannot_revive(db, status):
    task = seed(db, 1, status)
    event = create_generation_dispatch(db, task)
    event.status = "dead"
    db.commit()
    with pytest.raises(ValueError):
        replay_dead(db, event.id)
    result = tasks.dispatch_generation_items.apply(args=[task.id]).get()
    assert result["status"] == status and db.query(GenerationTaskItem).count() == 0


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "awaiting_review"])
def test_reconciliation_does_not_requeue_terminal_items(db, status):
    task = seed(db, 1, "running")
    row = items(db, task, status)[0]
    reconcile_generation(db)
    assert row.status == status and db.query(TaskOutbox).count() == 0


def test_broker_loss_sent_item_recovers_without_rerunning_finished_sibling(db):
    task = seed(db, 2, "dispatched")
    rows = items(db, task)
    rows[0].status = "succeeded"
    event = create_item_dispatch(db, task, rows[1])
    db.flush()
    old_sent(db, event)
    result = reconcile_generation(db)
    assert (
        result["redeliveries"] == 1 and event.status == "pending" and rows[0].status == "succeeded"
    )
    assert db.query(TaskOutbox).count() == 1


def test_missing_parent_message_and_missing_item_intents_are_repaired(db):
    task = seed(db, 2, "pending")
    event = create_generation_dispatch(db, task)
    db.flush()
    old_sent(db, event)
    assert reconcile_generation(db)["redeliveries"] == 1
    assert event.status == "pending"
    items(db, task)
    task.status = "dispatched"
    db.commit()
    reconcile_generation(db)
    assert db.query(TaskOutbox).filter_by(event_type="generation_item_dispatch").count() == 2


def test_running_lock_is_not_stolen_even_when_timestamp_stale(db):
    task = seed(db, 1, "running")
    row = items(db, task, "running")[0]
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    with item_execution_lock(db, row.thread_id) as owned:
        assert owned
        result = reconcile_generation(db)
        assert result["busy_items"] == 1 and row.status == "running"
    result = reconcile_generation(db)
    assert result["recovered_items"] == 1 and row.status == "pending"


def test_running_recovery_attempt_budget_is_finite(db):
    task = seed(db, 1, "running")
    row = items(db, task, "running")[0]
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    row.attempts = settings.TASK_MAX_RETRIES + 1
    db.commit()
    reconcile_generation(db)
    assert row.status == "failed" and row.failure_code == "RECOVERY_EXHAUSTED"


def test_retry_deadline_prevents_early_recovery_or_execution(db, monkeypatch):
    task = seed(db, 1, "running")
    row = items(db, task)[0]
    row.retry_after = datetime.now(timezone.utc) + timedelta(hours=2)
    db.commit()
    monkeypatch.setattr(tasks, "run_generation", lambda *a, **kw: pytest.fail("not due"))
    assert tasks.generate_single_item.apply(args=[task.id, 0]).get()["status"] == "retry_not_due"
    reconcile_generation(db)
    assert db.query(TaskOutbox).count() == 0


def test_cancellation_preserves_success_and_cancels_unstarted(db):
    task = seed(db, 2, "dispatched")
    rows = items(db, task)
    rows[0].status = "succeeded"
    task.cancel_requested_at = datetime.now(timezone.utc)
    db.commit()
    reconcile_generation(db)
    assert rows[0].status == "succeeded" and rows[1].status == "cancelled"
    assert tasks.generate_single_item.apply(args=[task.id, 0]).get()["status"] == "succeeded"


def test_committed_content_repairs_item_not_provider_reexecution(db):
    task = seed(db, 1, "running")
    row = items(db, task, "running")[0]
    content = ContentItem(
        task_id=task.id,
        template_id="fixture",
        thread_id=row.thread_id,
        payload={},
        status="pending_qc",
    )
    db.add(content)
    db.commit()
    reconcile_generation(db)
    assert (
        row.status == "succeeded"
        and row.content_id == content.id
        and db.query(TaskOutbox).count() == 0
    )


def test_manual_item_replay_only_delivery_failures(db):
    task = seed(db, 1, "failed")
    row = items(db, task, "failed")[0]
    event = create_item_dispatch(db, task, row)
    event.status = "dead"
    event.attempts = 3
    row.failure_code = "DELIVERY_EXHAUSTED"
    db.commit()
    replay_dead(db, event.id)
    assert task.status == "dispatched" and row.status == "pending"
    relay_pending(db, lambda *a, **kw: None, only_event_id=event.id)
    assert event.status == "sent"


def test_maintenance_scan_is_bounded(db, monkeypatch):
    seed(db, 1)
    another = GenerationTask(template_id="fixture", params={}, quantity=1, status="pending")
    db.add(another)
    db.commit()
    monkeypatch.setattr(settings, "GENERATION_RECONCILE_BATCH_SIZE", 1)
    assert reconcile_generation(db)["reconciled_tasks"] == 1
    assert db.query(TaskOutbox).count() == 1


def test_multiple_named_wrappers_are_not_silently_discarded():
    schema = {"type": "object", "required": ["stem", "answer"]}
    data = {"one": {"stem": "one", "answer": "A"}, "two": {"stem": "two", "answer": "B"}}
    assert _normalize_flat(schema, data) is data


def test_parent_expansion_dead_does_not_leave_missing_items_forever(db):
    task = seed(db, 3, "dispatched")
    row = GenerationTaskItem(
        task_id=task.id,
        item_index=0,
        thread_id=f"{task.id}:0",
        status="succeeded",
        tenant_id=task.tenant_id,
    )
    db.add(row)
    event = create_generation_dispatch(db, task)
    event.status = "dead"
    db.commit()
    result = reconcile_generation(db)
    assert result["dead_targets"] == 2
    rows = db.query(GenerationTaskItem).filter_by(task_id=task.id).all()
    assert len(rows) == 3 and row.status == "succeeded"
    assert task.status == "partially_succeeded" and task.progress == 1


def test_dead_targets_stop_and_explicit_replay_returns_to_delivery(db):
    task = seed(db, 1, "dispatched")
    row = items(db, task)[0]
    event = create_item_dispatch(db, task, row)
    event.status = "dead"
    db.commit()
    reconcile_generation(db)
    assert task.status == "failed" and row.failure_code == "DELIVERY_EXHAUSTED"
    replay_dead(db, event.id)
    assert task.status == "dispatched" and row.status == "pending"
    assert relay_pending(db, lambda *a, **kw: None, only_event_id=event.id)["sent"] == 1


def test_lost_delivery_is_bounded_not_infinite_sent_replays(db):
    task = seed(db, 1, "dispatched")
    row = items(db, task)[0]
    event = create_item_dispatch(db, task, row)
    old_sent(db, event)
    event.attempts = settings.OUTBOX_MAX_ATTEMPTS
    db.commit()
    reconcile_generation(db)
    assert event.status == "dead" and task.status == "failed"
    assert row.failure_code == "DELIVERY_EXHAUSTED"


def test_targeted_replay_does_not_send_unrelated_pending_task(db):
    task = seed(db, 2, "dispatched")
    rows = items(db, task)
    events = [create_item_dispatch(db, task, row) for row in rows]
    events[0].status = "dead"
    db.commit()
    replay_dead(db, events[0].id)
    calls = []
    relay_pending(
        db, lambda name, args, **kw: calls.append(args[1]), only_event_id=events[0].id, limit=1
    )
    assert calls == [0] and events[1].status == "pending"


def test_stale_cancelled_running_item_closes_without_provider(db):
    task = seed(db, 1, "running")
    row = items(db, task, "running")[0]
    row.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    task.cancel_requested_at = datetime.now(timezone.utc)
    db.commit()
    reconcile_generation(db)
    assert row.status == "cancelled" and task.status == "cancelled"


def test_relay_skips_cancelled_item_even_if_old_event_pending(db):
    task = seed(db, 1, "dispatched")
    row = items(db, task, "cancelled")[0]
    create_item_dispatch(db, task, row)
    db.commit()
    result = relay_pending(db, lambda *a, **kw: pytest.fail("cancelled message must not send"))
    assert result["skipped"] == 1


def test_service_accepts_five_but_worker_graph_prompt_always_single(db, monkeypatch):
    from app.workflow import graph

    seed(db)
    captured = {}
    monkeypatch.setattr(
        graph,
        "_load_template",
        lambda *a: SimpleNamespace(type_id="fixture", run_config={"rag": {"mode": "off"}}),
    )
    monkeypatch.setattr(
        graph,
        "generate_with_fallback",
        lambda template, params, *a, **kw: captured.update(params) or {},
    )
    params = {"quantity": 5, "template_id": "fixture"}
    graph.generate_node({"params": params, "trace_id": "fixture:0", "task_id": "fixture"}, db)
    assert captured["quantity"] == 1 and params["quantity"] == 5


def test_compose_has_aof_named_volume_and_always_on_beat():
    from pathlib import Path

    import yaml

    data = yaml.safe_load(
        (Path(__file__).parents[2] / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    redis = data["services"]["redis"]
    assert "redis-data:/data" in redis["volumes"]
    assert redis["command"] == ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
    assert "profiles" not in data["services"]["ocr-scheduler"]


def test_corrupted_item_envelope_does_not_send_sibling_or_foreign_scope(db):
    task = seed(db, 2, "dispatched")
    rows = items(db, task)
    event = create_item_dispatch(db, task, rows[0])
    event.payload = {"task_id": task.id, "item_index": 1}
    db.commit()
    result = relay_pending(db, lambda *a, **kw: pytest.fail("Wrong item must not send"))
    assert result["skipped"] == 1


def test_publish_runs_outside_database_transaction(db):
    task = seed(db, 1)
    event = create_generation_dispatch(db, task)
    db.commit()

    def send(*a, **kw):
        assert not db.in_transaction()

    assert relay_pending(db, send, only_event_id=event.id)["sent"] == 1


def test_missing_items_do_not_finish_parent_early(db):
    task = seed(db, 3, "running")
    row = GenerationTaskItem(
        task_id=task.id, item_index=0, thread_id=f"{task.id}:0", status="succeeded"
    )
    db.add(row)
    db.commit()
    tasks._update_parent_task_progress(db, task.id)
    assert task.status == "running" and task.progress == 0.3333
