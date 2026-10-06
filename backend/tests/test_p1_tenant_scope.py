from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.database import get_db
from app.main import create_app
from app.models import (
    AppNotification,
    ContentItem,
    GenerationTask,
    KnowledgeChunk,
    ModelProfile,
    QualityEvaluation,
    QualityRecord,
    QuestionTemplate,
    SamplePool,
    User,
)
from app.security import get_current_user


@pytest.fixture
def tenants(db, monkeypatch):
    # Stub only broker publication. API permission, SQL scopes and response schemas are real.
    monkeypatch.setattr(routes.celery_app, "send_task", lambda *args, **kwargs: None)
    users = {}
    for tenant, role in [("a", "researcher"), ("b", "researcher"), (None, "researcher")]:
        user = User(
            id=f"u-{tenant}",
            username=f"u-{tenant}",
            password_hash="test-only",
            role=role,
            tenant_id=tenant,
            status="active",
            display_name="Test",
        )
        db.add(user)
        users[tenant] = user
    db.add(
        QuestionTemplate(
            type_id="shared",
            name="Shared",
            version=1,
            input_schema={"type": "object"},
            output_schema={},
            quality_rules=[{"id": "a", "weight": 1}],
            gen_prompt={},
            run_config={"model_profile": "standard"},
            tenant_id=None,
        )
    )
    db.add(
        ModelProfile(name="standard", provider="test", model_name="shared-model", is_default=True)
    )
    db.flush()
    tasks, content, chunks, samples = {}, {}, {}, {}
    for tenant in ("a", "b", None):
        db.add(
            QuestionTemplate(
                type_id=f"template-{tenant}",
                name="Private",
                version=1,
                input_schema={},
                output_schema={},
                quality_rules=[],
                gen_prompt={},
                run_config={},
                tenant_id=tenant,
            )
        )
        db.add(
            ModelProfile(
                name=f"profile-{tenant}",
                provider="test",
                model_name=f"model-{tenant}",
                tenant_id=tenant,
            )
        )
        task = GenerationTask(
            template_id="shared", quantity=1, params={"owner": tenant}, tenant_id=tenant
        )
        db.add(task)
        db.flush()
        tasks[tenant] = task
        for status in ("published", "pending_qc"):
            item = ContentItem(
                task_id=task.id,
                template_id="shared",
                payload={"owner": tenant},
                status=status,
                tenant_id=tenant,
                thread_id=f"{task.id}:{status}",
            )
            db.add(item)
            db.flush()
            content[tenant, status] = item
            db.add(
                QualityRecord(
                    item_id=item.id,
                    source="auto",
                    score=80,
                    dimension_scores={"a": 80},
                    tenant_id=tenant,
                    config_snapshot={"threshold": 70},
                )
            )
        chunk = KnowledgeChunk(
            source_type="教材",
            source_name=f"source-{tenant}",
            content=f"private-{tenant}",
            embedding=[0.0] * 1024,
            tenant_id=tenant,
        )
        db.add(chunk)
        db.flush()
        chunks[tenant] = chunk
        sample = SamplePool(
            item_id=content[tenant, "published"].id,
            template_id="shared",
            payload={"owner": tenant},
            tenant_id=tenant,
        )
        db.add(sample)
        db.flush()
        samples[tenant] = sample
        db.add(
            AppNotification(
                type="task", title=f"note-{tenant}", content="test", tenant_id=tenant, is_read=False
            )
        )
        from app.models import TraceLog

        db.add(
            TraceLog(
                trace_id=f"trace-{tenant}",
                task_id=task.id,
                tenant_id=tenant,
                model=f"model-{tenant}",
                cost=1,
                stage="generate",
                usage_reported=True,
                prompt_cost=0.5,
                completion_cost=0.5,
                prompt_tokens=10,
                completion_tokens=5,
            )
        )
        db.add(
            QualityEvaluation(
                task_id=task.id,
                template_id="shared",
                thread_id=f"{task.id}:0",
                revise_count=0,
                score=80,
                status="completed",
                threshold=70,
                dimension_scores={"a": 80},
                config_snapshot={},
                is_final=True,
                tenant_id=tenant,
            )
        )
    db.commit()
    app = create_app()
    actor = {"user": users["a"]}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor["user"]
    client = TestClient(app)
    return SimpleNamespace(
        db=db,
        client=client,
        actor=actor,
        users=users,
        tasks=tasks,
        content=content,
        chunks=chunks,
        samples=samples,
    )


@pytest.mark.parametrize(
    "query",
    ["", "?status=published", "?template_id=shared", "?status=pending_qc&template_id=shared"],
)
def test_content_list_filters_are_scoped_and_callable(tenants, query):
    response = tenants.client.get("/api/contents" + query)
    assert response.status_code == 200, response.text
    assert all(item["payload"]["owner"] == "a" for item in response.json()["items"])


@pytest.mark.parametrize(
    "operation",
    [
        "detail",
        "publish",
        "review",
        "task",
        "cancel",
        "knowledge-delete",
        "sample-delete",
        "sample-create",
    ],
)
def test_cross_tenant_ids_cannot_be_used_for_reads_or_writes(tenants, operation):
    c = tenants.content["b", "published"].id
    t = tenants.tasks["b"].id
    actions = {
        "detail": lambda: tenants.client.get(f"/api/contents/{c}"),
        "publish": lambda: tenants.client.post(f"/api/contents/{c}/publish"),
        "review": lambda: tenants.client.post(f"/api/quality/{c}/review", json={"pass": True}),
        "task": lambda: tenants.client.get(f"/api/tasks/{t}"),
        "cancel": lambda: tenants.client.post(f"/api/tasks/{t}/cancel"),
        "knowledge-delete": lambda: tenants.client.delete(
            f"/api/knowledge/{tenants.chunks['b'].id}"
        ),
        "sample-delete": lambda: tenants.client.delete(f"/api/samples/{tenants.samples['b'].id}"),
        "sample-create": lambda: tenants.client.post("/api/samples", json={"content_id": c}),
    }
    response = actions[operation]()
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("tenant", ["a", None])
def test_task_cost_dashboard_trace_and_null_tenant_do_not_bypass_scope(tenants, tenant):
    tenants.actor["user"] = tenants.users[tenant]
    assert {item["id"] for item in tenants.client.get("/api/tasks").json()["items"]} == {
        tenants.tasks[tenant].id
    }
    costs = tenants.client.get("/api/costs?group_by=model").json()
    assert [row["name"] for row in costs] == [f"model-{tenant}"]
    deep = tenants.client.get("/api/costs/deep").json()
    assert deep["total_cost"] == 1 and len(deep["by_task"]) == 1
    dashboard = tenants.client.get("/api/dashboard").json()
    assert dashboard["generated_count"] == 2 and dashboard["total_cost"] == 1
    assert dashboard["quality_pipeline"]["threads"] == 1
    assert {trace["trace_id"] for trace in tenants.client.get("/api/traces").json()["items"]} == {
        f"trace-{tenant}"
    }
    assert (
        tenants.client.get(f"/api/traces/trace-{'b' if tenant != 'b' else 'a'}").status_code == 404
    )
    assert tenants.client.get("/api/traces/structured-stats").json()["total_generations"] == 1


def test_shared_configuration_is_visible_but_other_private_config_is_not(tenants):
    templates = {row["type_id"] for row in tenants.client.get("/api/templates").json()}
    profiles = {row["name"] for row in tenants.client.get("/api/model-profiles").json()}
    assert "shared" in templates and "template-a" in templates and "template-b" not in templates
    assert "standard" in profiles and "profile-a" in profiles and "profile-b" not in profiles
    response = tenants.client.post(
        "/api/generate", json={"template_id": "template-b", "params": {}, "quantity": 1}
    )
    assert response.status_code == 403


def test_generation_ignores_forged_tenant_and_duplicate_scope_is_per_owner(tenants):
    body = {
        "template_id": "shared",
        "quantity": 1,
        "params": {"tenant_id": "b", "topic": "x"},
        "tenant_id": "b",
    }
    response = tenants.client.post("/api/generate", json=body)
    assert response.status_code == 200, response.text
    task = tenants.db.get(GenerationTask, response.json()["task_id"])
    assert task.tenant_id == "a" and "tenant_id" not in task.params
    assert tenants.client.post("/api/generate", json=body).status_code == 409
    tenants.actor["user"] = tenants.users["b"]
    other = tenants.client.post("/api/generate", json=body)
    assert other.status_code == 200, other.text
    assert other.json()["task_id"] != task.id


def test_viewer_only_sees_published_content_and_not_task_parameters(tenants):
    viewer = SimpleNamespace(id="viewer", role="viewer", status="active", tenant_id="a")
    tenants.actor["user"] = viewer
    items = tenants.client.get("/api/contents").json()["items"]
    assert len(items) == 1 and items[0]["status"] == "published"
    assert (
        tenants.client.get(f"/api/contents/{tenants.content['a', 'pending_qc'].id}").status_code
        == 403
    )
    assert tenants.client.get("/api/tasks").status_code == 403
    assert tenants.client.get("/api/traces").status_code == 403


def test_knowledge_samples_exports_and_notifications_are_scoped(tenants):
    assert [row["source_name"] for row in tenants.client.get("/api/knowledge").json()["items"]] == [
        "source-a"
    ]
    assert all(
        row["payload"]["owner"] == "a" for row in tenants.client.get("/api/samples").json()["items"]
    )
    assert '"owner": "b"' not in tenants.client.get("/api/samples/export").text
    assert tenants.client.get("/api/notifications/unread-count").json()["count"] == 1
    notifications = tenants.client.get("/api/notifications").json()["items"]
    assert len(notifications) == 1 and notifications[0]["title"] == "note-a"
    assert tenants.client.post("/api/notifications/read-all").status_code == 200
    assert tenants.client.get("/api/notifications/unread-count").json()["count"] == 0
    assert tenants.db.query(AppNotification).filter_by(tenant_id="b", is_read=False).count() == 1


def test_worker_uses_task_owner_not_params_and_all_workflow_records_are_scoped(
    tenants, monkeypatch
):
    from app.config import settings
    from app.models import GenerationTaskItem
    from app.worker import tasks as worker
    from app.workflow import graph as wf

    task = GenerationTask(
        template_id="shared", params={"tenant_id": "b"}, quantity=1, tenant_id="a"
    )
    tenants.db.add(task)
    tenants.db.flush()
    tenants.db.add(
        GenerationTaskItem(task_id=task.id, item_index=0, thread_id=f"{task.id}:0", tenant_id="a")
    )
    tenants.db.commit()
    seen = []
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    wf.reset_checkpointer()
    monkeypatch.setattr(
        wf,
        "generate_with_fallback",
        lambda *args, **kwargs: seen.append(kwargs["tenant_id"]) or {"stem": "test"},
    )
    monkeypatch.setattr(
        wf,
        "run_quality_check",
        lambda *args, **kwargs: seen.append(kwargs["tenant_id"]) or (80, {"a": 80}),
    )
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, None))
    result = worker.generate_single_item.apply(args=[task.id, 0]).get()
    assert result["status"] == "succeeded" and seen == ["a", "a"]
    item = tenants.db.get(ContentItem, result["content_id"])
    assert item.tenant_id == "a"
    assert tenants.db.query(QualityRecord).filter_by(item_id=item.id).one().tenant_id == "a"
    assert (
        tenants.db.query(QualityEvaluation).filter_by(thread_id=f"{task.id}:0").one().tenant_id
        == "a"
    )
    wf.reset_checkpointer()


def test_null_owner_dispatch_does_not_invent_default_string(tenants, monkeypatch):
    from app.models import GenerationTaskItem
    from app.worker import tasks

    monkeypatch.setattr(tasks.generate_single_item, "delay", lambda *args: None)
    tasks.dispatch_generation_items.apply(args=[tenants.tasks[None].id]).get()
    assert (
        tenants.db.query(GenerationTaskItem)
        .filter_by(task_id=tenants.tasks[None].id)
        .one()
        .tenant_id
        is None
    )


def test_admin_is_explicit_global_operator_not_accidental_null_scope(tenants):
    tenants.actor["user"] = SimpleNamespace(
        id="admin", role="admin", status="active", tenant_id=None
    )
    assert tenants.client.get("/api/contents").json()["total"] == 6
    assert tenants.client.get("/api/tasks").json()["total"] == 3
    assert tenants.client.get("/api/costs/deep").json()["total_cost"] == 3
    assert tenants.client.get("/api/knowledge").json()["total"] == 3
    assert tenants.client.get("/api/notifications").json()["total"] == 3


def test_quality_stats_and_calibration_are_tenant_specific(tenants):
    from app.calibration import get_effective_weights
    from app.models import QualityCalibration

    for tenant, threshold in [("a", 80), ("b", 90), (None, 60)]:
        tenants.db.add(
            QualityCalibration(
                template_id="shared",
                weights={"a": 1},
                threshold=threshold,
                default_weights={"a": 1},
                default_threshold=70,
                lenient={},
                tenant_id=tenant,
            )
        )
    tenants.db.commit()
    response = tenants.client.get("/api/quality/stats?tenant_id=b")
    assert response.status_code == 200, response.text
    assert {bucket["tenant_id"] for bucket in response.json()["buckets"]} == {"a"}
    response = tenants.client.get("/api/quality/calibration?template_id=shared")
    assert [row["threshold"] for row in response.json()] == [80]
    assert get_effective_weights(tenants.db, "shared", tenant_id="a")[1] == 80
    assert get_effective_weights(tenants.db, "shared", tenant_id=None)[1] == 60


def test_cost_deep_requested_filters_apply_to_every_summary(tenants):
    task = tenants.tasks["a"]
    result = tenants.client.get(f"/api/costs/deep?task_id={task.id}").json()
    assert result["total_cost"] == 1 and [row["name"] for row in result["by_task"]] == [task.id]
    result = tenants.client.get(f"/api/costs/deep?task_id={tenants.tasks['b'].id}").json()
    assert result["total_count"] == 0 and result["by_task"] == [] and result["by_model"] == []


def test_login_and_admin_user_management_contracts_are_callable(tenants, monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(auth_service, "verify_password", lambda *_args: True)
    response = tenants.client.post(
        "/api/auth/login", json={"username": "u-a", "password": "test-only-password"}
    )
    assert response.status_code == 200 and response.json()["access_token"]
    assert "password_hash" not in response.json()["user"]
    tenants.actor["user"] = SimpleNamespace(
        id="admin", role="admin", status="active", tenant_id=None
    )
    response = tenants.client.get("/api/users?page=1&page_size=2")
    assert (
        response.status_code == 200
        and response.json()["total"] == 3
        and len(response.json()["items"]) == 2
    )


def test_cost_components_are_frozen_not_repriced_with_global_rate(tenants, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "COST_PER_1K_TOKENS", 999)
    response = tenants.client.get("/api/costs/deep").json()
    assert (
        response["stages"][0]["prompt_cost"] == 0.5
        and response["stages"][0]["completion_cost"] == 0.5
    )


def test_global_admin_review_keeps_record_owned_by_content_not_reviewer_tenant(tenants):
    tenants.actor["user"] = SimpleNamespace(
        id="global-admin-b", role="admin", status="active", tenant_id="b"
    )
    content = tenants.content["a", "pending_qc"]
    response = tenants.client.post(f"/api/quality/{content.id}/review", json={"pass": True})
    assert response.status_code == 200, response.text
    record = (
        tenants.db.query(QualityRecord).filter_by(item_id=content.id, source="manual_review").one()
    )
    assert record.tenant_id == "a" and record.reviewer == "global-admin-b"


def test_only_global_admin_can_explicitly_select_calibration_tenant(tenants):
    denied = tenants.client.post(
        "/api/quality/calibrate", json={"template_id": "shared", "tenant_id": "b"}
    )
    assert denied.status_code == 403, denied.text
    tenants.actor["user"] = SimpleNamespace(
        id="global-admin", role="admin", status="active", tenant_id=None
    )
    allowed = tenants.client.post(
        "/api/quality/calibrate", json={"template_id": "shared", "tenant_id": "a"}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["tenant_id"] == "a"


def test_actual_jwt_resolves_database_tenant_and_disabled_account(tenants, monkeypatch):
    from app.config import settings
    from app.security import create_access_token

    monkeypatch.setattr(
        settings,
        "JWT_SECRET",
        "test-jwt-secret-that-is-long-enough-1234567890",  # gitleaks:allow - synthetic test value
    )  # gitleaks:allow - fixed test assertion/credential, not a live secret
    tenants.client.app.dependency_overrides.pop(get_current_user)
    token = create_access_token(tenants.users["a"])
    headers = {"Authorization": f"Bearer {token}"}
    response = tenants.client.get("/api/contents", headers=headers)
    assert response.status_code == 200
    assert all(item["payload"]["owner"] == "a" for item in response.json()["items"])
    assert (
        tenants.client.get(f"/api/tasks/{tenants.tasks['b'].id}", headers=headers).status_code
        == 403
    )
    tenants.users["a"].status = "disabled"
    tenants.db.commit()
    assert tenants.client.get("/api/contents", headers=headers).status_code == 401
