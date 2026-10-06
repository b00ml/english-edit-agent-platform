# app/worker/tasks.py —— Celery 批量生成任务（P1-3 重构：取消 + 并发）
from datetime import datetime, timedelta, timezone

from celery import shared_task
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal

# Public legacy aliases all refer to the shared task vocabulary.
from app.domain.status import (
    DELIVERY_PROTECTED_ITEM_STATUSES,
)
from app.domain.status import TASK_CANCELLED as CANCELLED
from app.domain.status import TASK_DISPATCHED as DISPATCHED
from app.domain.status import TASK_FAILED as FAILED
from app.domain.status import TASK_PARTIALLY_SUCCEEDED as PARTIALLY_SUCCEEDED
from app.domain.status import TASK_PENDING as PENDING
from app.domain.status import TASK_RUNNING as RUNNING
from app.domain.status import TASK_SUCCEEDED as SUCCEEDED
from app.domain.status import aggregate_item_statuses, item_status_from_graph
from app.engine.trace import record_lifecycle_event
from app.models import GenerationTask, GenerationTaskItem
from app.notification import notify_task_result
from app.outbox import create_item_dispatch, relay_pending
from app.worker.execution_lock import item_execution_lock
from app.worker.failure_policy import classify_failure
from app.workflow.graph import run_generation


@shared_task(
    name="app.worker.tasks.relay_task_outbox",
    bind=True,
    max_retries=settings.TASK_MAX_RETRIES,
    autoretry_for=(RedisConnectionError, RedisTimeoutError, OperationalError),
    retry_backoff=True,
    acks_late=True,
)
def relay_task_outbox(self) -> dict:
    """投递数据库 Outbox；发送失败按 attempts 进入 dead，供管理员重放。"""
    from app.worker.celery_app import celery_app

    session = SessionLocal()
    try:
        result = relay_pending(
            session,
            lambda name, args, task_id=None: celery_app.send_task(name, args=args, task_id=task_id),
        )
        return result
    finally:
        session.close()


def _item_sender(name: str, args: list, task_id: str | None = None) -> object:
    from app.worker.celery_app import celery_app

    return celery_app.send_task(name, args=args, task_id=task_id)


@shared_task(name="app.worker.tasks.maintain_generation", acks_late=True)
def maintain_generation() -> dict:
    from app.worker.celery_app import celery_app
    from app.worker.recovery import reconcile_generation

    session = SessionLocal()
    try:
        from app.engine.trace_snapshots import expire_snapshots

        expired = expire_snapshots(session)
        session.commit()
        result = reconcile_generation(session)
        result["expired_trace_snapshots"] = expired
        result.update(
            relay_pending(
                session,
                lambda name, args, task_id=None: celery_app.send_task(
                    name, args=args, task_id=task_id
                ),
            )
        )
        return result
    finally:
        session.close()


@shared_task(
    name="app.worker.tasks.dispatch_generation_items",
    bind=True,
    max_retries=settings.TASK_MAX_RETRIES,
    autoretry_for=(RedisConnectionError, RedisTimeoutError, OperationalError),
    retry_backoff=True,
    default_retry_delay=60,
    acks_late=True,
)
def dispatch_generation_items(self, task_id: str) -> dict:
    """调度器：将批量任务拆分为独立 item，投递到 Celery 队列。

    职责：
    1. 检查任务是否已取消
    2. 为每个 item 创建 GenerationTaskItem 记录
    3. 投递 N 个独立的 generate_single_item 任务
    4. 更新父任务状态为 dispatched
    """
    trace_id = f"task:{task_id}"
    started = datetime.now(timezone.utc)
    record_lifecycle_event(
        trace_id=trace_id,
        stage="queue_dispatch",
        event="started",
        model="celery",
        task_id=task_id,
    )
    session = SessionLocal()
    try:
        task = (
            session.query(GenerationTask)
            .filter(GenerationTask.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            record_lifecycle_event(
                trace_id=trace_id,
                stage="queue_dispatch",
                event="failed",
                model="celery",
                task_id=task_id,
                status=FAILED,
                reason="任务不存在",
                success=False,
            )
            return {"task_id": task_id, "status": FAILED, "reason": "任务不存在"}

        trace_id = task.trace_ref or trace_id
        task.trace_ref = trace_id

        # Completed/awaiting work must never be resurrected by an old parent message.
        if task.status not in {PENDING, DISPATCHED, RUNNING}:
            return {"task_id": task_id, "status": task.status, "skipped": True}

        # 检查是否已取消
        if task.cancel_requested_at:
            task.status = CANCELLED
            session.commit()
            record_lifecycle_event(
                trace_id=trace_id,
                stage="queue_dispatch",
                event="finished",
                model="celery",
                task_id=task_id,
                status=CANCELLED,
                reason="任务已在调度前取消",
                success=False,
            )
            return {"task_id": task_id, "status": CANCELLED, "reason": "任务已在调度前取消"}

        quantity = task.quantity
        tenant_id = task.tenant_id

        # Items and their dispatch intents commit together. A partial broker
        # outage cannot leave an item without a durable delivery record.
        for idx in range(quantity):
            item = (
                session.query(GenerationTaskItem).filter_by(task_id=task_id, item_index=idx).first()
            )
            if item is None:
                item = GenerationTaskItem(
                    task_id=task_id,
                    item_index=idx,
                    thread_id=f"{task_id}:{idx}",
                    status=PENDING,
                    tenant_id=tenant_id,
                )
                session.add(item)
                session.flush()
            if item.status == PENDING:
                create_item_dispatch(session, task, item)
        if task.status == PENDING:
            task.status = DISPATCHED
        session.commit()
        relay_pending(session, _item_sender)

        record_lifecycle_event(
            trace_id=trace_id,
            stage="queue_dispatch",
            event="finished",
            model="celery",
            task_id=task_id,
            status=DISPATCHED,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            metadata={"item_count": quantity},
        )
        return {
            "task_id": task_id,
            "status": DISPATCHED,
            "total": quantity,
            "message": f"{quantity} 个 item 已投递到队列",
        }
    except Exception as exc:  # noqa: BLE001 - record queue failure then preserve retry semantics
        record_lifecycle_event(
            trace_id=trace_id,
            stage="queue_dispatch",
            event="failed",
            model="celery",
            task_id=task_id,
            status=FAILED,
            reason=str(exc),
            success=False,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
        )
        raise
    finally:
        session.close()


@shared_task(
    name="app.worker.tasks.process_generation_task",
    bind=True,
    max_retries=settings.TASK_MAX_RETRIES,
    autoretry_for=(RedisConnectionError, RedisTimeoutError, OperationalError),
    retry_backoff=True,
    default_retry_delay=60,
    acks_late=True,
)
def process_generation_task(self, task_id: str) -> dict:
    """兼容旧任务名，将旧批量入口转发到 item 调度器。"""
    return dispatch_generation_items.run(task_id)


@shared_task(
    name="app.worker.tasks.generate_single_item",
    bind=True,
    max_retries=settings.TASK_MAX_RETRIES,
    autoretry_for=(RedisConnectionError, RedisTimeoutError, OperationalError),
    retry_backoff=True,
    default_retry_delay=60,
    acks_late=True,
)
def generate_single_item(self, task_id: str, item_index: int) -> dict:
    """单 item worker：检查取消 → 执行生成 → 更新状态。

    幂等性：通过 GenerationTaskItem.thread_id 唯一约束 + PostgresSaver checkpoint 保证。
    失败分类：
    - 结构化 SDK/HTTP 4xx：永久失败；408/429/5xx 与连接超时：有限重试
    - 未知异常：失败关闭，不凭消息中的数字分类
    - 基础设施错误（Redis/DB）：自动重试（autoretry_for）
    """
    trace_id = f"{task_id}:{item_index}"
    session = SessionLocal()
    try:
        task = session.query(GenerationTask).filter(GenerationTask.id == task_id).first()
        if task is None:
            record_lifecycle_event(
                trace_id=trace_id,
                stage="queue_item",
                event="failed",
                model="celery",
                task_id=task_id,
                item_id=trace_id,
                status=FAILED,
                reason="父任务不存在",
                success=False,
            )
            return {
                "task_id": task_id,
                "item_index": item_index,
                "status": FAILED,
                "reason": "父任务不存在",
            }

        if not 0 <= item_index < task.quantity:
            return {"task_id": task_id, "status": "invalid_item_index"}

        with item_execution_lock(session, trace_id) as owned:
            if not owned:
                return {"task_id": task_id, "item_index": item_index, "status": "already_running"}
            session.expire_all()
            existing = (
                session.query(GenerationTaskItem)
                .filter_by(task_id=task_id, item_index=item_index)
                .first()
            )
            if existing is not None and existing.status in DELIVERY_PROTECTED_ITEM_STATUSES:
                return {
                    "task_id": task_id,
                    "item_index": item_index,
                    "status": existing.status,
                    "skipped": True,
                    "reason": "item 已完成，跳过重复执行",
                }
            if task.status not in {PENDING, DISPATCHED, RUNNING}:
                return {
                    "task_id": task_id,
                    "item_index": item_index,
                    "status": task.status,
                    "skipped": True,
                }

            # 检查父任务是否已取消
            if task.cancel_requested_at:
                # 更新 item 状态为 cancelled（如果记录存在）
                item = (
                    session.query(GenerationTaskItem)
                    .filter(
                        GenerationTaskItem.task_id == task_id,
                        GenerationTaskItem.item_index == item_index,
                    )
                    .first()
                )
                if item:
                    item.status = CANCELLED
                    session.commit()
                    _update_parent_task_progress(session, task_id)
                record_lifecycle_event(
                    trace_id=trace_id,
                    stage="queue_item",
                    event="finished",
                    model="celery",
                    task_id=task_id,
                    item_id=item.id if item else None,
                    status=CANCELLED,
                    reason="父任务已取消",
                    success=False,
                )
                return {
                    "task_id": task_id,
                    "item_index": item_index,
                    "status": CANCELLED,
                    "reason": "父任务已取消",
                }

            # 获取或创建 GenerationTaskItem 记录
            thread_id = f"{task_id}:{item_index}"
            item = (
                session.query(GenerationTaskItem)
                .filter(
                    GenerationTaskItem.task_id == task_id,
                    GenerationTaskItem.item_index == item_index,
                )
                .first()
            )

            if not item:
                # 兜底创建（理论上 dispatch 阶段已创建）
                item = GenerationTaskItem(
                    task_id=task_id,
                    item_index=item_index,
                    thread_id=thread_id,
                    status=PENDING,
                    tenant_id=task.tenant_id,
                )
                session.add(item)
                session.commit()

            # 检查 item 是否已完成（幂等性保护）
            if item.status in DELIVERY_PROTECTED_ITEM_STATUSES:
                record_lifecycle_event(
                    trace_id=item.thread_id,
                    stage="queue_item",
                    event="skipped",
                    model="celery",
                    task_id=task_id,
                    item_id=item.id,
                    status=item.status,
                    reason="item 已完成，跳过重复执行",
                )
                return {
                    "task_id": task_id,
                    "item_index": item_index,
                    "status": item.status,
                    "reason": "item 已完成，跳过重复执行",
                }

            if item.retry_after and item.retry_after.replace(tzinfo=timezone.utc) > datetime.now(
                timezone.utc
            ):
                return {"task_id": task_id, "status": "retry_not_due"}
            item.retry_after = None
            # 更新 item 状态为 running
            item.status = RUNNING
            task.status = RUNNING
            item.started_at = datetime.now(timezone.utc)
            item.attempts += 1
            session.commit()
            item_started = datetime.now(timezone.utc)
            record_lifecycle_event(
                trace_id=item.thread_id,
                stage="queue_item",
                event="started",
                model="celery",
                task_id=task_id,
                item_id=item.id,
                template_id=task.template_id,
                tenant_id=item.tenant_id,
                metadata={"item_index": item_index, "attempt": item.attempts},
            )

            # 调用 workflow 生成
            try:
                result = run_generation(
                    task_id=task_id,
                    template_id=task.template_id,
                    params={
                        **task.params,
                        **({"quantity": 1} if "quantity" in task.params else {}),
                        "tenant_id": task.tenant_id,
                    },
                    session=session,
                    thread_id=thread_id,
                )
                final_status = result.get("status")

                item.status = item_status_from_graph(
                    str(final_status or ""), bool(result.get("__interrupt__"))
                )
                item.content_id = result.get("content_id")
                if item.status == FAILED:
                    item.failure_code = result.get("failure_code") or "QUALITY_REJECTED"
                    item.failure_reason = (
                        result.get("failure_reason") or result.get("reason") or "质检未通过"
                    )
                elif item.status == CANCELLED:
                    item.failure_reason = result.get("reason", "任务已取消")

                item.finished_at = (
                    None if item.status == "awaiting_review" else datetime.now(timezone.utc)
                )
                session.commit()

            except (
                Exception
            ) as exc:  # noqa: BLE001 - one item failure is persisted without aborting the batch
                session.rollback()
                decision = classify_failure(exc)
                exhausted = (
                    self.request.retries >= self.max_retries
                    or item.attempts >= self.max_retries + 1
                )
                retry = decision.retryable and not exhausted
                item.status = PENDING if retry else FAILED
                item.retry_after = (
                    (datetime.now(timezone.utc) + timedelta(seconds=2 ** min(item.attempts, 8)))
                    if retry
                    else None
                )
                item.failure_code = (
                    "RETRY_EXHAUSTED" if decision.retryable and exhausted else decision.code
                )
                item.failure_reason = type(exc).__name__
                if not retry:
                    item.finished_at = datetime.now(timezone.utc)
                session.commit()
                record_lifecycle_event(
                    trace_id=item.thread_id,
                    stage="queue_item",
                    event="retry_scheduled" if retry else "failed",
                    model="celery",
                    task_id=task_id,
                    item_id=item.id,
                    template_id=task.template_id,
                    tenant_id=item.tenant_id,
                    status="retrying" if retry else FAILED,
                    reason=item.failure_reason,
                    success=False,
                    metadata={"failure_code": item.failure_code, "retries": self.request.retries},
                )
                if retry:
                    raise self.retry(countdown=2 ** min(item.attempts, 8), exc=exc)
                _update_parent_task_progress(session, task_id)
                return {
                    "task_id": task_id,
                    "item_index": item_index,
                    "status": FAILED,
                    "failure_code": item.failure_code,
                    "reason": item.failure_reason,
                }

            # 原子性更新父任务进度
            _update_parent_task_progress(session, task_id)

            record_lifecycle_event(
                trace_id=item.thread_id,
                stage="queue_item",
                event="finished",
                model="celery",
                task_id=task_id,
                item_id=item.id,
                template_id=task.template_id,
                tenant_id=item.tenant_id,
                status=item.status,
                success=item.status not in {FAILED, CANCELLED},
                latency_ms=(datetime.now(timezone.utc) - item_started).total_seconds() * 1000,
                metadata={"item_index": item_index},
            )

            return {
                "task_id": task_id,
                "item_index": item_index,
                "status": item.status,
                "content_id": item.content_id,
            }

    finally:
        session.close()


def _update_parent_task_progress(session: Session, task_id: str) -> None:
    """原子性更新父任务进度与状态。"""
    # 先锁父任务，再读取子任务状态，避免并发完成时写回旧的进度。
    task = (
        session.query(GenerationTask).filter(GenerationTask.id == task_id).with_for_update().first()
    )
    if task is None:
        return
    # 统计各状态 item 数量
    items = session.query(GenerationTaskItem).filter(GenerationTaskItem.task_id == task_id).all()
    if not items:
        return

    next_status, progress = aggregate_item_statuses([i.status for i in items], task.quantity)
    task.progress = progress
    if task.status != CANCELLED and next_status is not None and task.status != next_status:
        task.status = next_status
        if next_status in {SUCCEEDED, PARTIALLY_SUCCEEDED, FAILED, CANCELLED}:
            notify_task_result(session, task)

    session.commit()
