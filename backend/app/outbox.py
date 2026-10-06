"""Durable parent/item dispatch with fenced SQL claims and bounded replay cycles."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.status import DELIVERY_PROTECTED_ITEM_STATUSES, DELIVERY_TASK_STATUSES
from app.models import GenerationTask, GenerationTaskItem, TaskOutbox

logger = logging.getLogger("app.outbox")
OutboxSender = Callable[..., Any]
ITEM_TERMINAL = frozenset(DELIVERY_PROTECTED_ITEM_STATUSES)
TASK_ACTIVE = frozenset(DELIVERY_TASK_STATUSES)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_generation_dispatch(session: Session, task: GenerationTask) -> TaskOutbox:
    key = f"task:{task.id}:dispatch"
    existing = session.query(TaskOutbox).filter_by(event_id=key).first()
    if existing is not None:
        return existing
    event = TaskOutbox(
        event_id=key,
        task_id=task.id,
        event_type="generation_dispatch",
        payload={"task_id": task.id},
        status="pending",
        tenant_id=task.tenant_id,
    )
    session.add(event)
    return event


def create_item_dispatch(
    session: Session, task: GenerationTask, item: GenerationTaskItem
) -> TaskOutbox:
    key = f"item:{item.thread_id}:dispatch"
    existing = session.query(TaskOutbox).filter_by(event_id=key).first()
    if existing is not None:
        return existing
    event = TaskOutbox(
        event_id=key,
        task_id=task.id,
        item_id=item.id,
        event_type="generation_item_dispatch",
        payload={"task_id": task.id, "item_index": item.item_index},
        status="pending",
        tenant_id=task.tenant_id,
    )
    session.add(event)
    return event


def _send_event(event: TaskOutbox, sender: OutboxSender) -> None:
    if event.event_type == "generation_item_dispatch":
        name, args = "app.worker.tasks.generate_single_item", [
            event.task_id,
            int(event.payload["item_index"]),
        ]
    elif event.event_type == "generation_dispatch":
        name, args = "app.worker.tasks.process_generation_task", [event.task_id]
    else:
        raise ValueError("Unsupported Outbox event type")
    sender(name, args, task_id=event.event_id)


def _eligible(session: Session, event: TaskOutbox) -> bool:
    task = session.get(GenerationTask, event.task_id)
    if (
        task is None
        or task.cancel_requested_at is not None
        or task.status not in TASK_ACTIVE
        or event.tenant_id != task.tenant_id
    ):
        return False
    if event.event_type == "generation_item_dispatch":
        item = session.get(GenerationTaskItem, event.item_id)
        return (
            item is not None
            and item.task_id == task.id
            and item.tenant_id == task.tenant_id
            and item.status == "pending"
            and event.payload.get("task_id") == task.id
            and event.payload.get("item_index") == item.item_index
        )
    return event.event_type == "generation_dispatch"


def reset_delivery(event: TaskOutbox, instant: datetime) -> None:
    """Start a new bounded delivery cycle, preserving total attempt audit history."""
    event.status = "pending"
    event.available_at = instant
    event.retry_base = event.attempts
    event.lease_token = None
    event.lease_until = None


def _claim(session: Session, only_event_id: str | None = None) -> tuple[str, str] | None:
    instant = utcnow()
    stale = instant - timedelta(seconds=settings.OUTBOX_SENDING_TIMEOUT_SECONDS)
    expired = (
        select(TaskOutbox.id)
        .where(
            TaskOutbox.status == "sending",
            or_(
                TaskOutbox.lease_until <= instant,
                and_(TaskOutbox.lease_until.is_(None), TaskOutbox.updated_at <= stale),
            ),
        )
        .order_by(TaskOutbox.updated_at)
        .limit(settings.OUTBOX_RELAY_BATCH_SIZE)
    )
    if only_event_id is not None:
        expired = expired.where(TaskOutbox.id == only_event_id)
    if session.get_bind().dialect.name == "postgresql":
        expired = expired.with_for_update(skip_locked=True)
    expired_ids = list(session.scalars(expired))
    if expired_ids:
        session.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id.in_(expired_ids))
            .values(status="pending", lease_token=None, lease_until=None, available_at=instant)
        )
    stmt = (
        select(TaskOutbox)
        .where(TaskOutbox.status == "pending", TaskOutbox.available_at <= instant)
        .order_by(TaskOutbox.created_at, TaskOutbox.id)
        .limit(1)
    )
    if only_event_id is not None:
        stmt = stmt.where(TaskOutbox.id == only_event_id)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    event = session.scalar(stmt)
    if event is None:
        session.commit()
        return None
    token = str(uuid.uuid4())
    changed = session.execute(
        update(TaskOutbox)
        .where(TaskOutbox.id == event.id, TaskOutbox.status == "pending")
        .values(
            status="sending",
            attempts=TaskOutbox.attempts + 1,
            lease_token=token,
            lease_until=instant + timedelta(seconds=settings.OUTBOX_SENDING_TIMEOUT_SECONDS),
        )
    ).rowcount
    identifier = event.id
    session.commit()
    return (identifier, token) if changed else None


def _ack(session: Session, identifier: str, token: str, values: dict[str, Any]) -> bool:
    changed = session.execute(
        update(TaskOutbox)
        .where(
            TaskOutbox.id == identifier,
            TaskOutbox.status == "sending",
            TaskOutbox.lease_token == token,
        )
        .values(**values, lease_token=None, lease_until=None)
    ).rowcount
    session.commit()
    return bool(changed)


def relay_pending(
    session: Session,
    sender: OutboxSender,
    *,
    limit: int | None = None,
    only_event_id: str | None = None,
) -> dict[str, int]:
    counts = {"sent": 0, "dead": 0, "scanned": 0, "skipped": 0, "stale": 0}
    for _ in range(limit if limit is not None else settings.OUTBOX_RELAY_BATCH_SIZE):
        claimed = _claim(session, only_event_id)
        if claimed is None:
            break
        identifier, token = claimed
        event = session.get(TaskOutbox, identifier)
        counts["scanned"] += 1
        if event is None:
            continue
        if event.attempts - (event.retry_base or 0) > settings.OUTBOX_MAX_ATTEMPTS:
            _ack(
                session,
                identifier,
                token,
                {"status": "dead", "last_error": "DeliveryAttemptBudgetExhausted"},
            )
            counts["dead"] += 1
            continue
        if not _eligible(session, event):
            _ack(session, identifier, token, {"status": "cancelled"})
            counts["skipped"] += 1
            continue
        # Snapshot the envelope before commit; expired ORM attributes must not
        # silently reopen a DB transaction during a slow external publish.
        wire = TaskOutbox(
            event_id=event.event_id,
            task_id=event.task_id,
            event_type=event.event_type,
            payload=dict(event.payload),
        )
        cycle_attempt = event.attempts - (event.retry_base or 0)
        session.commit()
        try:
            _send_event(wire, sender)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - persist only sanitized error type, preserve event for retry.
            dead = cycle_attempt >= settings.OUTBOX_MAX_ATTEMPTS
            ok = _ack(
                session,
                identifier,
                token,
                {
                    "status": "dead" if dead else "pending",
                    "last_error": type(exc).__name__,
                    "available_at": utcnow()
                    + timedelta(
                        seconds=settings.OUTBOX_RETRY_BACKOFF_SECONDS
                        * 2 ** min(max(cycle_attempt - 1, 0), 8)
                    ),
                },
            )
            if ok and dead:
                counts["dead"] += 1
            if not ok:
                counts["stale"] += 1
            logger.warning(
                "Outbox delivery failed event=%s error=%s", identifier, type(exc).__name__
            )
            continue
        if _ack(
            session, identifier, token, {"status": "sent", "sent_at": utcnow(), "last_error": None}
        ):
            counts["sent"] += 1
        else:
            counts["stale"] += 1
    return counts


def replay_dead(session: Session, event_id: str) -> TaskOutbox:
    event = session.query(TaskOutbox).filter_by(id=event_id).with_for_update().first()
    if event is None or event.status != "dead":
        raise ValueError("Outbox event absent or is not dead")
    task = session.get(GenerationTask, event.task_id)
    if (
        task is None
        or task.cancel_requested_at is not None
        or task.status in {"succeeded", "cancelled", "awaiting_review"}
    ):
        raise ValueError("Cannot replay a completed or cancelled target")
    if task.request_hash:
        from app.domain.status import ACTIVE_TASK_STATUSES

        competing = (
            session.query(GenerationTask)
            .filter(
                GenerationTask.request_hash == task.request_hash,
                GenerationTask.id != task.id,
                GenerationTask.status.in_(ACTIVE_TASK_STATUSES),
            )
            .first()
        )
        if competing is not None:
            raise ValueError("同一请求已有活动任务，不能复活旧死信目标")
    if event.event_type == "generation_item_dispatch":
        item = session.get(GenerationTaskItem, event.item_id)
        if (
            item is None
            or item.status in {"succeeded", "cancelled", "awaiting_review"}
            or item.content_id
        ):
            raise ValueError("Cannot replay completed or reviewed content")
        if (
            item.status == "running"
            or item.status == "failed"
            and item.failure_code != "DELIVERY_EXHAUSTED"
        ):
            raise ValueError("Only delivery-exhausted failed items may be explicitly replayed")
        item.status, item.retry_after, item.finished_at = "pending", None, None
    elif event.event_type == "generation_dispatch":
        if session.query(GenerationTaskItem).filter_by(task_id=task.id).count():
            raise ValueError(
                "Replay individual item dispatches, not an already expanded failed parent"
            )
    else:
        raise ValueError("Unsupported event type")
    task.status = "pending" if event.event_type == "generation_dispatch" else "dispatched"
    reset_delivery(event, utcnow())
    session.commit()
    return event
