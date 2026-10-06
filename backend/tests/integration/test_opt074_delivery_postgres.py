"""Migrated original CI PostgreSQL: fenced claims, concurrent relay and restartable intents."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import GenerationTask, GenerationTaskItem, QuestionTemplate, TaskOutbox
from app.outbox import (
    _ack,
    _claim,
    create_item_dispatch,
    relay_pending,
    replay_dead,
)
from app.worker.execution_lock import item_execution_lock
from app.worker.recovery import reconcile_generation

pytestmark = pytest.mark.integration


def seed(session, quantity=2):
    key = "delivery-" + uuid.uuid4().hex
    template = QuestionTemplate(
        type_id=key,
        name="Test",
        version=1,
        input_schema={},
        output_schema={},
        quality_rules=[],
        gen_prompt={},
        run_config={},
    )
    session.add(template)
    session.flush()
    task = GenerationTask(
        template_id=key,
        quantity=quantity,
        params={"quantity": 1},
        status="dispatched",
        tenant_id=key,
    )
    session.add(task)
    session.flush()
    rows = [
        GenerationTaskItem(
            task_id=task.id,
            item_index=i,
            thread_id=f"{task.id}:{i}",
            status="pending",
            tenant_id=key,
        )
        for i in range(quantity)
    ]
    session.add_all(rows)
    session.flush()
    return task, rows


def test_migrated_columns_and_single_claim_owner(pg_session):
    cols = {c["name"] for c in inspect(pg_session.get_bind()).get_columns("task_outbox")}
    assert {"retry_base", "lease_token", "lease_until"} <= cols
    assert "retry_after" in {
        c["name"] for c in inspect(pg_session.get_bind()).get_columns("generation_task_item")
    }
    task, rows = seed(pg_session)
    event = create_item_dispatch(pg_session, task, rows[0])
    pg_session.commit()
    first = _claim(pg_session, event.id)
    assert first and _claim(pg_session, event.id) is None
    event = pg_session.get(TaskOutbox, event.id)
    event.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    pg_session.commit()
    second = _claim(pg_session, event.id)
    assert second and first[1] != second[1]
    assert not _ack(pg_session, first[0], first[1], {"status": "sent"})
    assert _ack(pg_session, second[0], second[1], {"status": "sent"})


def test_try_worker_lock_is_nonblocking_across_connections(pg_session, pg_engine):
    key = "lock-" + uuid.uuid4().hex
    other = sessionmaker(bind=pg_engine)()
    try:
        with item_execution_lock(pg_session, key) as owned:
            assert owned
            with item_execution_lock(other, key) as duplicate:
                assert not duplicate
        with item_execution_lock(other, key) as owned:
            assert owned
    finally:
        other.close()


def test_real_pg_partial_failure_replay_and_sent_message_loss(pg_session, monkeypatch):
    monkeypatch.setattr(settings, "GENERATION_DELIVERY_TIMEOUT_SECONDS", 60)
    task, rows = seed(pg_session)
    events = [create_item_dispatch(pg_session, task, r) for r in rows]
    pg_session.commit()

    def sender(name, args, **kw):
        if args[1] == 1:
            raise ConnectionError("test outage")

    for e in events:
        relay_pending(pg_session, sender, only_event_id=e.id, limit=1)
    pg_session.refresh(events[1])
    assert events[1].status == "pending"
    events[1].status = "dead"
    pg_session.commit()
    replay_dead(pg_session, events[1].id)
    assert relay_pending(pg_session, lambda *a, **kw: None, only_event_id=events[1].id)["sent"] == 1
    rows[0].status = "succeeded"
    events[1].sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    pg_session.commit()
    reconcile_generation(pg_session)
    pg_session.refresh(events[1])
    assert events[1].status == "pending" and rows[0].status == "succeeded"


def test_concurrent_relays_and_late_ack_have_one_owner(pg_engine):
    # This test needs independent committed transactions (not a shared rollback
    # envelope). It creates one UUID scope and deletes only that scope afterwards.
    factory = sessionmaker(bind=pg_engine)
    identifier = template_id = task_id = None
    started = threading.Event()
    finish = threading.Event()
    calls = []
    errors = []
    try:
        with factory() as session:
            task, rows = seed(session, 1)
            event = create_item_dispatch(session, task, rows[0])
            session.commit()
            identifier, template_id, task_id = event.id, task.template_id, task.id

        def delayed_sender(*args, **kw):
            calls.append(kw["task_id"])
            started.set()
            if not finish.wait(10):
                raise TimeoutError("test release timeout")

        def relay():
            try:
                with factory() as s:
                    relay_pending(s, delayed_sender, only_event_id=identifier, limit=1)
            except BaseException as e:
                errors.append(type(e).__name__)

        worker = threading.Thread(target=relay)
        worker.start()
        assert started.wait(10)
        with factory() as other:
            result = relay_pending(
                other, lambda *a, **kw: calls.append("duplicate"), only_event_id=identifier, limit=1
            )
            assert result["scanned"] == 0
        finish.set()
        worker.join(15)
        assert not worker.is_alive() and not errors
        assert len(calls) == 1
        with factory() as s:
            assert s.get(TaskOutbox, identifier).status == "sent"
    finally:
        finish.set()
        if task_id:
            with factory() as s:
                s.query(GenerationTask).filter_by(id=task_id).delete()
                s.query(QuestionTemplate).filter_by(type_id=template_id).delete()
                s.commit()


def test_actual_pg_dispatch_item_replay_yields_exact_five_single_outputs(pg_session, monkeypatch):
    from app.models import ContentItem
    from app.worker import tasks

    task, rows = seed(pg_session, 5)
    task.params = {"quantity": 5}
    pg_session.commit()
    monkeypatch.setattr(tasks, "SessionLocal", lambda: pg_session)
    monkeypatch.setattr(tasks, "record_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(pg_session, "close", lambda: None)
    inputs = []

    def generated(**kw):
        inputs.append(kw["params"]["quantity"])
        item = ContentItem(
            task_id=task.id,
            template_id=task.template_id,
            thread_id=kw["thread_id"],
            payload={"stem": "fixture"},
            status="pending_qc",
            tenant_id=task.tenant_id,
        )
        pg_session.add(item)
        pg_session.commit()
        return {"status": "stored", "content_id": item.id}

    monkeypatch.setattr(tasks, "run_generation", generated)

    def send(name, args, **kw):
        assert name == "app.worker.tasks.generate_single_item"
        return tasks.generate_single_item.apply(args=args).get()

    monkeypatch.setattr(tasks, "_item_sender", send)
    tasks.dispatch_generation_items.apply(args=[task.id]).get()
    assert inputs == [1] * 5
    assert pg_session.query(ContentItem).filter_by(task_id=task.id).count() == 5
    for row in rows:
        result = tasks.generate_single_item.apply(args=[task.id, row.item_index]).get()
        assert result["status"] == "succeeded"
    assert inputs == [1] * 5
    assert task.status == "succeeded" and task.progress == 1
    # Parent duplicate and periodic reconciliation also cannot trigger new calls.
    tasks.dispatch_generation_items.apply(args=[task.id]).get()
    reconcile_generation(pg_session)
    assert inputs == [1] * 5
