from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.errors import NotFoundError, TenantScopeDeniedError
from app.models import AppNotification, ContentItem, GenerationTask
from app.notification import (
    build_task_notification,
    notify_content_rejected,
    notify_human_review,
    notify_task_result,
)
from app.services.generation_service import GenerationService
from app.services.knowledge_service import KnowledgeService
from app.services.sample_service import SampleService
from app.services.trace_service import TraceService


def _user(tenant_id="tenant-1"):
    return SimpleNamespace(id="u1", tenant_id=tenant_id, role="researcher")


def _seed_current(db):
    from app.models import QuestionTemplate

    template = QuestionTemplate(
        type_id="fixture",
        name="Fixture",
        version=1,
        input_schema={},
        output_schema={},
        quality_rules=[],
        gen_prompt={},
        run_config={},
    )
    db.add(template)
    db.flush()
    task = GenerationTask(
        template_id=template.type_id,
        params={"knowledge_point": "noun"},
        quantity=1,
        status="succeeded",
        tenant_id="tenant-1",
    )
    db.add(task)
    db.flush()
    item = ContentItem(
        task_id=task.id,
        template_id=template.type_id,
        payload={"stem": "sample"},
        status="passed",
        tenant_id="tenant-1",
    )
    db.add(item)
    db.commit()
    return task, item


def test_knowledge_service_upload_and_query_paths(db, monkeypatch):
    from app.rag.parser import parse_text

    diagnostics = {}

    def index(session, *args, **kwargs):
        diagnostics.update(kwargs)
        kwargs.get("details", {}).update(leaf_count=2, diagnostics={})
        return 2

    monkeypatch.setattr("app.services.knowledge_service.index_document", index)
    service = KnowledgeService(db)
    assert service.upload_text("hello", "教材", "book", "noun", {}, _user())["chunks"] == 2
    monkeypatch.setattr(
        "app.services.knowledge_service.parse_document",
        lambda *a, **kw: parse_text("book.txt", "synthetic text"),
    )
    assert service.upload_file("book.txt", b"text", "教材", "noun", _user())["chunks"] == 2
    with pytest.raises(Exception):
        service.upload_file("", b"", "教材", None, _user())


def test_knowledge_service_crud_and_retrieve(db, monkeypatch):
    from app.models import KnowledgeChunk

    row = KnowledgeChunk(
        content="synthetic", source_name="test", source_type="text", tenant_id="tenant-1"
    )
    db.add(row)
    db.commit()
    service = KnowledgeService(db)
    assert service.list_knowledge(None, 1, 20, _user())["total"] == 1
    monkeypatch.setattr(
        "app.services.knowledge_service.retrieve", lambda *a, **kw: [{"text": "synthetic"}]
    )
    assert service.retrieve_knowledge("noun", None, 3, _user())["snippets"]
    with pytest.raises(TenantScopeDeniedError):
        service.delete_knowledge(row.id, _user("tenant-2"))
    service.delete_knowledge(row.id, _user())
    with pytest.raises(NotFoundError):
        service.delete_knowledge(row.id, _user())


def test_sample_service_paths(db, monkeypatch):
    task, item = _seed_current(db)
    service = SampleService(db)
    row = service.create_sample_from_content(item.id, "manual", "fewshot", _user())
    assert service.list_samples_filtered(None, None, None, None, 1, 10, _user())[1] == 1
    assert service.sync_samples(_user())["added"] == 0
    assert row.payload == item.payload
    assert service.delete_sample_by_id(row.id, _user())["deleted"] == row.id
    with pytest.raises(NotFoundError):
        service.delete_sample_by_id(row.id, _user())


def test_sample_service_legacy_paths(db):
    task, item = _seed_current(db)
    service = SampleService(db)
    row = service.create_sample_from_content(item.id, "manual", "sft", _user())
    with pytest.raises(TenantScopeDeniedError):
        service.delete_sample_by_id(row.id, _user("tenant-2"))
    assert service.list_samples_filtered(None, None, None, None, 1, 10, _user())[1] == 1


def test_trace_service_reports():
    now = datetime.now(timezone.utc)
    trace = SimpleNamespace(
        trace_id="tr1",
        model="m1",
        total_cost=0.2,
        input_tokens=2,
        output_tokens=3,
        operation="generate",
        latency_ms=10,
        created_at=now,
    )

    class Traces:
        def list_by_tenant(self, **kwargs):
            return [trace]

        def count_by_tenant(self, **kwargs):
            return 1

        def aggregate_by_model(self, **kwargs):
            return [{"total_cost": 0.2, "total_tokens": 5}]

        def aggregate_by_date(self, **kwargs):
            return []

        def sum_cost(self, **kwargs):
            return 0.2

        def structured_output_stats(self, **kwargs):
            return {"total": 2, "success_rate": 0.5, "retry_rate": 0.5, "by_model": []}

        def get_by_trace_id(self, **kwargs):
            return [
                SimpleNamespace(
                    id="1",
                    trace_id="tr1",
                    model="m1",
                    operation="generate",
                    input_tokens=1,
                    output_tokens=2,
                    total_cost=0.1,
                    latency_ms=3,
                    prompt="p",
                    response="r",
                    error=None,
                    created_at=now,
                )
            ]

    class Tasks:
        def count_by_status(self, **kwargs):
            return {"succeeded": 1}

    class Contents:
        def count_status_summary(self, **kwargs):
            return {"passed": 1}

    service = TraceService(SimpleNamespace())
    service.trace_repo = Traces()
    service.task_repo = Tasks()
    service.content_repo = Contents()
    user = _user()
    assert service.list_costs(user)[0]["trace_id"] == "tr1"
    assert service.get_deep_cost(user)["total_tokens"] == 5
    assert service.get_dashboard(user)["cost_7d"] == 0.2
    assert service.list_traces(user)["total"] == 1
    assert service.get_structured_stats(user)["retry_rate"] == 0.5
    assert service.get_trace_detail("tr1", user)[0]["prompt"] == "p"
    service.trace_repo.get_by_trace_id = lambda **kwargs: []
    assert service.get_trace_detail("missing", user) == []


def test_notification_build_and_idempotency(db):
    now = datetime.now(timezone.utc)
    task = GenerationTask(id="t1", quantity=2, tenant_id="tenant-1", status="succeeded")
    assert build_task_notification(task).type == "task_succeeded"
    task.status = "partially_succeeded"
    assert build_task_notification(task).type == "task_partial"
    task.status = "failed"
    assert build_task_notification(task).type == "task_failed"
    task.status = "running"
    assert build_task_notification(task) is None
    task.status = "succeeded"
    notify_task_result(db, task)
    notify_task_result(db, task)
    item = ContentItem(id="i1", tenant_id="tenant-1")
    notify_content_rejected(db, item, "bad")
    notify_content_rejected(db, item, "bad")
    notify_human_review(db, item, 65)
    notify_human_review(db, item, 65)
    assert db.query(AppNotification).count() == 3


def test_generation_service_query_and_status_contracts(db):
    task, item = _seed_current(db)
    service = GenerationService(db)
    assert service.get_task(task.id, _user()).id == task.id
    assert service.list_tasks(tenant_id="tenant-1", current_user=_user()).total == 1
    assert service.update_task_status(task.id, "running", 0.5).progress == 0.5
    with pytest.raises(Exception):
        service.get_task("missing")
