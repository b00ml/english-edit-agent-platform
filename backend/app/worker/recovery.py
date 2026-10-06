"""Bounded DB reconciliation of pending/dispatched/running tasks after broker loss."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.status import item_status_from_content
from app.models import ContentItem, GenerationTask, GenerationTaskItem, TaskOutbox
from app.outbox import ITEM_TERMINAL, TASK_ACTIVE, create_generation_dispatch, create_item_dispatch
from app.worker.execution_lock import item_execution_lock


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _redeliver(event: TaskOutbox, instant: datetime) -> bool:
    if event.status == "dead":
        return False  # Explicit operator replay only; do not reset exhausted history.
    if event.status in {"pending", "sending"}:
        return False
    reference = event.sent_at or event.updated_at or event.created_at
    if instant - aware(reference) < timedelta(seconds=settings.GENERATION_DELIVERY_TIMEOUT_SECONDS):
        return False
    if event.attempts - (event.retry_base or 0) >= settings.OUTBOX_MAX_ATTEMPTS:
        event.status = "dead"
        event.last_error = "DeliveryAcknowledgementTimeout"
        return False
    event.status = "pending"
    event.available_at = instant
    event.lease_token = None
    event.lease_until = None
    return True


def reconcile_generation(session: Session, now: datetime | None = None) -> dict[str, int]:
    from app.worker.tasks import _update_parent_task_progress

    instant = now or datetime.now(timezone.utc)
    counts = {
        "reconciled_tasks": 0,
        "recovered_items": 0,
        "redeliveries": 0,
        "cancelled_items": 0,
        "dead_targets": 0,
        "busy_items": 0,
    }
    # Oldest active tasks are examined first; updated_at changes per pass to avoid
    # a permanently dead first page starving other active tasks.
    identifiers = list(
        session.scalars(
            select(GenerationTask.id)
            .where(GenerationTask.status.in_(TASK_ACTIVE))
            .order_by(GenerationTask.updated_at, GenerationTask.id)
            .limit(settings.GENERATION_RECONCILE_BATCH_SIZE)
        )
    )
    session.commit()
    for identifier in identifiers:
        query = select(GenerationTask).where(GenerationTask.id == identifier)
        if session.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        task = session.scalar(query)
        if task is None or task.status not in TASK_ACTIVE:
            session.rollback()
            continue
        # Keep every acquired item ownership lock through its status commit.
        with ExitStack() as locks:
            items = list(
                session.scalars(
                    select(GenerationTaskItem).where(GenerationTaskItem.task_id == task.id)
                )
            )
            counts["reconciled_tasks"] += 1
            if not 1 <= task.quantity <= 50:
                # Reject inconsistent legacy metadata without allocating unbounded items.
                task.status, task.progress = "failed", 1.0
                session.commit()
                counts["dead_targets"] += 1
                continue
            if task.cancel_requested_at:
                for item in items:
                    if item.status not in ITEM_TERMINAL and item.status != "running":
                        item.status, item.finished_at = "cancelled", instant
                        counts["cancelled_items"] += 1
                if not items:
                    task.status, task.progress = "cancelled", 1.0
            elif len(items) < task.quantity:
                event = create_generation_dispatch(session, task)
                session.flush()
                counts["redeliveries"] += int(_redeliver(event, instant))
            for item in items:
                if item.status in ITEM_TERMINAL:
                    continue
                content = session.scalar(
                    select(ContentItem).where(ContentItem.thread_id == item.thread_id)
                )
                if content is not None:
                    item.content_id = content.id
                    item.status = item_status_from_content(content.status)
                    item.finished_at = instant
                    continue
                if task.cancel_requested_at:
                    if item.status == "running" and instant - aware(
                        item.started_at or item.updated_at
                    ) >= timedelta(seconds=settings.GENERATION_STALE_ITEM_SECONDS):
                        owned = locks.enter_context(item_execution_lock(session, item.thread_id))
                        if owned:
                            item.status, item.finished_at = "cancelled", instant
                            counts["cancelled_items"] += 1
                        else:
                            counts["busy_items"] += 1
                    continue
                if item.status == "running":
                    reference = item.started_at or item.updated_at
                    if instant - aware(reference) < timedelta(
                        seconds=settings.GENERATION_STALE_ITEM_SECONDS
                    ):
                        continue
                    owned = locks.enter_context(item_execution_lock(session, item.thread_id))
                    if not owned:
                        counts["busy_items"] += 1
                        continue
                    session.refresh(item)
                    if item.status != "running":
                        continue
                    if item.attempts >= settings.TASK_MAX_RETRIES + 1:
                        item.status, item.failure_code = "failed", "RECOVERY_EXHAUSTED"
                        item.failure_reason = "StaleExecutionBudgetExhausted"
                        item.finished_at = instant
                        continue
                    item.status, item.retry_after = "pending", None
                    counts["recovered_items"] += 1
                if (
                    item.status != "pending"
                    or item.retry_after
                    and aware(item.retry_after) > instant
                ):
                    continue
                event = create_item_dispatch(session, task, item)
                session.flush()
                counts["redeliveries"] += int(_redeliver(event, instant))
                if event.status == "dead":
                    item.status, item.failure_code = "failed", "DELIVERY_EXHAUSTED"
                    item.failure_reason = "DispatchFailedOrAcknowledgementMissing"
                    item.finished_at = instant
                    counts["dead_targets"] += 1
            # Never automatically revive an exhausted parent dispatch.
            parent_event = session.scalar(
                select(TaskOutbox).where(TaskOutbox.event_id == f"task:{task.id}:dispatch")
            )
            if not task.cancel_requested_at and parent_event and parent_event.status == "dead":
                present = {item.item_index for item in items}
                for index in range(task.quantity):
                    if index in present:
                        continue
                    missing = GenerationTaskItem(
                        task_id=task.id,
                        item_index=index,
                        thread_id=f"{task.id}:{index}",
                        status="failed",
                        tenant_id=task.tenant_id,
                        failure_code="DELIVERY_EXHAUSTED",
                        failure_reason="ParentExpansionExhausted",
                        finished_at=instant,
                    )
                    session.add(missing)
                    session.flush()
                    event = create_item_dispatch(session, task, missing)
                    event.status, event.last_error = "dead", "ParentExpansionExhausted"
                    items.append(missing)
                    counts["dead_targets"] += 1
            task.updated_at = instant
            session.commit()
            if items:
                _update_parent_task_progress(session, task.id)
    return counts


def recover_stale_tasks(session: Session, now: datetime | None = None) -> int:
    """Compatibility wrapper; recovery creates fenced delivery intents, not raw publishes."""
    result = reconcile_generation(session, now)
    return result["recovered_items"] + result["redeliveries"]
