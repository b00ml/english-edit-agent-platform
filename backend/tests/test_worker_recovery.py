# tests/test_worker_recovery.py —— 僵尸任务恢复 + Celery 可靠性配置单测（P0-6 / OPT-019）
from datetime import datetime, timezone

from app.config import settings
from app.worker.celery_app import celery_app
from app.worker.recovery import recover_stale_tasks
from app.worker.tasks import process_generation_task


class TestRecoverStaleTasks:
    def test_wrapper_uses_fenced_db_reconciliation(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "app.worker.recovery.reconcile_generation",
            lambda session, now=None: calls.append((session, now))
            or {"recovered_items": 2, "redeliveries": 3},
        )
        session = object()
        instant = datetime.now(timezone.utc)
        assert recover_stale_tasks(session, now=instant) == 5
        assert calls == [(session, instant)]

    def test_no_recovery_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            "app.worker.recovery.reconcile_generation",
            lambda *a, **kw: {"recovered_items": 0, "redeliveries": 0},
        )
        assert recover_stale_tasks(object()) == 0

    def test_beat_has_generation_maintenance(self):
        job = celery_app.conf.beat_schedule["generation-durable-reconcile"]
        assert job["task"] == "app.worker.tasks.maintain_generation"
        assert job["options"]["queue"] == "celery"


class TestCeleryReliabilityConfig:
    def test_task_delivery_semantics(self):
        # 执行完才 ack；worker 崩溃消息重投
        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.task_reject_on_worker_lost is True
        # 可见性超时须大于硬超时 900s
        assert celery_app.conf.broker_transport_options["visibility_timeout"] > 900

    def test_task_retry_semantics(self):
        assert process_generation_task.max_retries == settings.TASK_MAX_RETRIES
        assert process_generation_task.acks_late is True
        autoretry = getattr(process_generation_task, "autoretry_for", ())
        names = {getattr(e, "__name__", str(e)) for e in autoretry}
        assert "ConnectionError" in names
        assert "TimeoutError" in names
        assert "OperationalError" in names
