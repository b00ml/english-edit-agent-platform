"""Existing PostgreSQL: real concurrent admission, state constraints and UI model API."""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.errors import DuplicateTaskError
from app.models import (
    GenerationTask,
    GenerationTaskItem,
    ModelProfile,
    ModelProvider,
    QuestionTemplate,
    TaskOutbox,
)
from app.schemas import GenerateRequest
from app.services.generation_service import GenerationService
from app.worker import tasks
from app.worker.recovery import reconcile_generation

pytestmark = pytest.mark.integration


def template(key):
    return QuestionTemplate(
        type_id=key,
        name="Test",
        version=1,
        input_schema={},
        output_schema={
            "type": "object",
            "required": ["stem"],
            "properties": {"stem": {"type": "string"}},
        },
        quality_rules=[{"id": "correct", "weight": 1}],
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
        run_config={"max_retry": 2},
    )


def test_actual_partial_unique_and_status_constraints(pg_session):
    indices = {i["name"]: i for i in inspect(pg_session.get_bind()).get_indexes("generation_task")}
    assert indices["uq_active_generation_request"]["unique"]
    assert "awaiting_review" in str(indices["uq_active_generation_request"]["dialect_options"])
    assert "ck_generation_task_status" in {
        c["name"] for c in inspect(pg_session.get_bind()).get_check_constraints("generation_task")
    }
    assert "ck_generation_item_status" in {
        c["name"]
        for c in inspect(pg_session.get_bind()).get_check_constraints("generation_task_item")
    }
    assert {"model_provider", "model_route"} <= set(
        inspect(pg_session.get_bind()).get_table_names()
    )


def test_six_real_connections_race_create_one_task_and_outbox(pg_engine, monkeypatch):
    factory = sessionmaker(bind=pg_engine)
    key = "race-" + uuid.uuid4().hex
    with factory() as db:
        db.add(template(key))
        db.commit()
    barrier = threading.Barrier(6)
    from app.repositories import TaskRepository

    original = TaskRepository.get_by_request_hash

    def race(self, fingerprint):
        result = original(self, fingerprint)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(TaskRepository, "get_by_request_hash", race)
    monkeypatch.setattr("app.services.generation_service.relay_pending", lambda *a, **kw: None)

    def submit(_):
        with factory() as db:
            try:
                result = GenerationService(db).create_task(
                    GenerateRequest(template_id=key, params={}, quantity=2),
                    SimpleNamespace(role="admin", tenant_id=None),
                    Mock(),
                )
                return "accepted", result.task_id
            except DuplicateTaskError as exc:
                return "duplicate", exc.existing_task_id

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        assert [r[0] for r in results].count("accepted") == 1
        assert len({r[1] for r in results}) == 1
        monkeypatch.setattr(TaskRepository, "get_by_request_hash", original)
        with factory() as db:
            task = db.query(GenerationTask).filter(GenerationTask.template_id == key).one()
            assert db.query(TaskOutbox).filter(TaskOutbox.task_id == task.id).count() == 1
            task.status = "awaiting_review"
            db.commit()
            with pytest.raises(DuplicateTaskError):
                GenerationService(db).create_task(
                    GenerateRequest(template_id=key, params={}, quantity=2),
                    SimpleNamespace(role="admin", tenant_id=None),
                    Mock(),
                )
            task.status = "succeeded"
            db.commit()
            new = GenerationService(db).create_task(
                GenerateRequest(template_id=key, params={}, quantity=2),
                SimpleNamespace(role="admin", tenant_id=None),
                Mock(),
            )
            assert new.task_id != task.id
    finally:
        # These are exclusively our UUID fixture rows; never delete shared/history tasks.
        with factory() as db:
            ids = [
                r.id
                for r in db.query(GenerationTask).filter(GenerationTask.template_id == key).all()
            ]
            if ids:
                db.query(TaskOutbox).filter(TaskOutbox.task_id.in_(ids)).delete(
                    synchronize_session=False
                )
            db.query(GenerationTask).filter(GenerationTask.template_id == key).delete(
                synchronize_session=False
            )
            db.query(QuestionTemplate).filter(QuestionTemplate.type_id == key).delete(
                synchronize_session=False
            )
            db.commit()


def test_real_pg_waiting_review_survives_reconciliation_and_duplicate_delivery(
    pg_session, monkeypatch
):
    key = "waiting-" + uuid.uuid4().hex
    pg_session.add(template(key))
    pg_session.flush()
    task = GenerationTask(template_id=key, params={}, quantity=2, status="running")
    pg_session.add(task)
    pg_session.flush()
    rows = [
        GenerationTaskItem(task_id=task.id, item_index=i, thread_id=f"{task.id}:{i}", status=s)
        for i, s in enumerate(["succeeded", "awaiting_review"])
    ]
    pg_session.add_all(rows)
    pg_session.commit()
    monkeypatch.setattr(tasks, "record_lifecycle_event", lambda **kw: None)
    monkeypatch.setattr(tasks, "notify_task_result", lambda *a: None)
    tasks._update_parent_task_progress(pg_session, task.id)
    assert task.status == "awaiting_review" and task.progress == 0.5
    reconcile_generation(pg_session)
    assert task.status == "awaiting_review" and rows[1].status == "awaiting_review"
    assert pg_session.query(TaskOutbox).filter(TaskOutbox.task_id == task.id).count() == 0
    rows[1].status = "succeeded"
    pg_session.commit()
    tasks._update_parent_task_progress(pg_session, task.id)
    assert task.status == "succeeded" and task.progress == 1


def test_real_api_credentials_encrypted_routes_effective_without_paid_call(
    client, auth_headers, pg_app_sessions, monkeypatch
):
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", Fernet.generate_key().decode())
    key = "model-" + uuid.uuid4().hex
    secret = "private-test-credential-not-live"
    created = client.post(
        "/api/model-settings/providers",
        headers=auth_headers,
        json={"name": key, "base_url": "http://localhost:9876/v1", "api_key": secret},
    )
    assert created.status_code == 200, created.text
    assert secret not in created.text and "ciphertext" not in created.text
    pid = created.json()["id"]
    profile = client.post(
        "/api/model-profiles",
        headers=auth_headers,
        json={
            "name": key,
            "provider": "custom",
            "provider_id": pid,
            "model_name": "test-custom-model",
        },
    )
    assert profile.status_code == 200, profile.text
    with pg_app_sessions() as db:
        db.add(template(key))
        db.commit()
        row = db.get(ModelProvider, pid)
        assert secret not in row.api_key_ciphertext
    configured = client.post(
        "/api/model-settings/routes",
        headers=auth_headers,
        json={"template_id": key, "generation_profile": key, "judge_profile": key},
    )
    assert configured.status_code == 200
    from openai import OpenAI

    from app.engine import providers, quality, structured_output

    calls = []

    def transport(request):
        calls.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content) if request.content else {},
            }
        )
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "test-custom-model",
                            "object": "model",
                            "created": 0,
                            "owned_by": "test",
                        }
                    ],
                },
            )
        content = (
            '{"stem":"valid synthetic question"}'
            if "结构" in request.content.decode() or len(calls) == 1
            else '{"dimension_scores":{"correct":90}}'
        )
        # Generating is the first chat; Judge follows regardless of localized prompt text.
        if sum(c["url"].endswith("chat/completions") for c in calls) == 1:
            content = '{"stem":"valid synthetic question"}'
        else:
            content = '{"dimension_scores":{"correct":90}}'
        return httpx.Response(
            200,
            json={
                "id": "mock-call",
                "object": "chat.completion",
                "created": 0,
                "model": "test-custom-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

    monkeypatch.setattr(
        providers,
        "OpenAI",
        lambda **kw: OpenAI(
            **kw, http_client=httpx.Client(transport=httpx.MockTransport(transport))
        ),
    )
    with pg_app_sessions() as db:
        p = db.query(ModelProfile).filter(ModelProfile.name == key).one()
        t = db.query(QuestionTemplate).filter(QuestionTemplate.type_id == key).one()
        trace_id = key + ":0"
        assert structured_output.generate_structured(t, {}, p, trace_id=trace_id)["stem"]
        assert (
            quality.run_quality_check(
                t, {"stem": "x"}, model_profile=p, rounds=1, trace_id=trace_id
            )[0]
            == 90
        )
    probed = client.post(f"/api/model-settings/providers/{pid}/probe", headers=auth_headers)
    assert probed.status_code == 200 and probed.json()["generation_verified"] is False
    assert all(
        c["url"].startswith("http://localhost:9876/v1/")
        and c["authorization"] == "Bearer " + secret
        for c in calls
    )
    assert all(c["body"].get("model") == "test-custom-model" for c in calls if c["body"])
    audit = client.get("/api/config-audit", headers=auth_headers).text
    assert secret not in audit and "api_key_ciphertext" not in audit
    assert client.get("/api/model-settings/providers", headers=auth_headers).status_code == 200
    assert client.get("/api/model-settings/routes", headers=auth_headers).status_code == 200
    assert (
        client.post(
            "/api/model-settings/routes", headers=auth_headers, json={"template_id": key}
        ).status_code
        == 200
    )
