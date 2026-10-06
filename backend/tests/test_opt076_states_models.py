"""Cross-layer state contract, SQL admission guard and safe model configuration."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.api import model_settings as api
from app.api.routes import create_model_profile
from app.config import settings
from app.domain.status import (
    ACTIVE_TASK_STATUSES,
    DELIVERY_PROTECTED_ITEM_STATUSES,
    ITEM_STATUSES,
    TERMINAL_ITEM_STATUSES,
    aggregate_item_statuses,
    item_status_from_content,
    item_status_from_graph,
)
from app.engine import providers
from app.errors import DuplicateTaskError, InvalidGenerationStateError, ProviderConfigurationError
from app.models import (
    ConfigAuditEvent,
    GenerationTask,
    GenerationTaskItem,
    ModelProvider,
    QuestionTemplate,
    TaskOutbox,
    User,
)
from app.repositories import TaskRepository
from app.schemas import GenerateRequest, ModelProfileIn
from app.security import create_access_token
from app.services.generation_service import GenerationService
from app.worker import tasks


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(tasks, "record_lifecycle_event", lambda **kw: None)


def seed(db, quantity=2, status="pending", key=None):
    t = QuestionTemplate(
        type_id="fixture",
        name="Fixture",
        version=1,
        input_schema={},
        output_schema={},
        quality_rules=[],
        gen_prompt={},
        run_config={},
    )
    db.add(t)
    db.flush()
    task = GenerationTask(
        template_id=t.type_id, params={}, quantity=quantity, status=status, request_hash=key
    )
    db.add(task)
    db.commit()
    return task


def item_rows(db, task, statuses):
    rows = [
        GenerationTaskItem(task_id=task.id, item_index=i, thread_id=f"{task.id}:{i}", status=s)
        for i, s in enumerate(statuses)
    ]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.mark.parametrize(
    "status,expected",
    [
        ("stored", "succeeded"),
        ("review_passed", "succeeded"),
        ("rejected", "failed"),
        ("review_rejected", "failed"),
        ("awaiting_review", "awaiting_review"),
        ("cancelled", "cancelled"),
        ("failed", "failed"),
    ],
)
def test_graph_mapping_is_explicit(status, expected):
    assert item_status_from_graph(status) == expected


@pytest.mark.parametrize("status", ["finished", "succeeded", "generated", "", "new_unknown"])
def test_unknown_graph_never_falls_through_to_success(status):
    with pytest.raises(InvalidGenerationStateError):
        item_status_from_graph(status)
    assert item_status_from_graph(status, True) == "awaiting_review"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("pending_qc", "succeeded"),
        ("passed", "succeeded"),
        ("published", "succeeded"),
        ("rejected", "failed"),
        ("awaiting_review", "awaiting_review"),
    ],
)
def test_content_reconciliation_mapping(status, expected):
    assert item_status_from_content(status) == expected
    with pytest.raises(InvalidGenerationStateError):
        item_status_from_content("garbage")


@pytest.mark.parametrize(
    "statuses,total,expected,progress",
    [
        (["awaiting_review"], 1, "awaiting_review", 0),
        (["succeeded", "awaiting_review"], 2, "awaiting_review", 0.5),
        (["failed", "awaiting_review"], 2, "awaiting_review", 0.5),
        (["pending", "awaiting_review"], 2, None, 0),
        (["running", "succeeded"], 2, None, 0.5),
        (["succeeded"], 3, None, 0.3333),
        (["succeeded", "failed"], 2, "partially_succeeded", 1),
        (["succeeded", "cancelled"], 2, "partially_succeeded", 1),
        (["failed", "cancelled"], 2, "failed", 1),
        (["cancelled", "cancelled"], 2, "cancelled", 1),
        (["succeeded", "succeeded"], 2, "succeeded", 1),
    ],
)
def test_parent_aggregation(statuses, total, expected, progress):
    assert aggregate_item_statuses(statuses, total) == (expected, progress)


def test_review_is_active_and_delivery_protected_not_terminal():
    assert "awaiting_review" in ACTIVE_TASK_STATUSES
    assert "awaiting_review" in DELIVERY_PROTECTED_ITEM_STATUSES
    assert "awaiting_review" not in TERMINAL_ITEM_STATUSES
    assert set(ITEM_STATUSES) == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "awaiting_review",
    }
    with pytest.raises(InvalidGenerationStateError):
        aggregate_item_statuses(["unknown"], 1)
    with pytest.raises(InvalidGenerationStateError):
        aggregate_item_statuses([], 0)


def test_parent_waits_then_resolves_once_without_cancel_resurrection(db, monkeypatch):
    task = seed(db)
    rows = item_rows(db, task, ["succeeded", "awaiting_review"])
    notice = Mock()
    monkeypatch.setattr(tasks, "notify_task_result", notice)
    tasks._update_parent_task_progress(db, task.id)
    assert task.status == "awaiting_review" and task.progress == 0.5 and not notice.called
    rows[1].status = "failed"
    db.commit()
    tasks._update_parent_task_progress(db, task.id)
    tasks._update_parent_task_progress(db, task.id)
    assert task.status == "partially_succeeded" and notice.call_count == 1
    task.status = "cancelled"
    db.commit()
    rows[1].status = "succeeded"
    db.commit()
    tasks._update_parent_task_progress(db, task.id)
    assert task.status == "cancelled"


def test_invalid_graph_worker_result_is_failed_not_success(db, monkeypatch):
    task = seed(db, quantity=1, status="dispatched")
    rows = item_rows(db, task, ["pending"])
    monkeypatch.setattr(tasks, "run_generation", lambda **kw: {"status": "mystery"})
    result = tasks.generate_single_item.apply(args=[task.id, 0]).get()
    assert result["status"] == "failed" and rows[0].failure_code == "INVALID_GENERATION_STATE"
    assert task.status == "failed"


@pytest.mark.parametrize("status", ["queued", "stored", "rejected", "mystery"])
def test_sql_rejects_noncanonical_item_status(db, status):
    task = seed(db, quantity=1)
    db.add(
        GenerationTaskItem(task_id=task.id, item_index=0, thread_id=task.id + ":0", status=status)
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize("status", ["completed", "stored", "mystery"])
def test_sql_rejects_noncanonical_task_status(db, status):
    task = seed(db)
    task.status = status
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize("status", ["pending", "dispatched", "running", "awaiting_review"])
def test_active_hash_sql_uniqueness_and_repository_contract(db, status):
    task = seed(db, status=status, key="same")
    assert TaskRepository(db).get_by_request_hash("same").id == task.id
    db.add(
        GenerationTask(
            template_id=task.template_id,
            params={},
            quantity=1,
            status="pending",
            request_hash="same",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize("status", ["succeeded", "partially_succeeded", "failed", "cancelled"])
def test_terminal_hash_is_reusable(db, status):
    task = seed(db, status=status, key="same")
    assert TaskRepository(db).get_by_request_hash("same") is None
    new = GenerationTask(
        template_id=task.template_id, params={}, quantity=1, status="pending", request_hash="same"
    )
    db.add(new)
    db.commit()
    assert new.id != task.id


def test_integrity_race_returns_winning_task_and_no_extra_outbox(db, monkeypatch):
    seed(db, status="succeeded")
    user = SimpleNamespace(role="admin", tenant_id=None)
    monkeypatch.setattr("app.services.generation_service.relay_pending", lambda *a, **kw: None)
    req = GenerateRequest(template_id="fixture", params={}, quantity=2)
    service = GenerationService(db)
    first = service.create_task(req, user, Mock())
    monkeypatch.setattr(service.task_repo, "get_by_request_hash", lambda *a: None)
    with pytest.raises(DuplicateTaskError) as caught:
        service.create_task(req, user, Mock())
    assert caught.value.existing_task_id == first.task_id
    assert db.query(TaskOutbox).count() == 1


def test_template_timestamp_rewrite_does_not_bypass_dedup(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    seed(db, status="succeeded")
    monkeypatch.setattr("app.services.generation_service.relay_pending", lambda *a, **kw: None)
    service = GenerationService(db)
    req = GenerateRequest(template_id="fixture", params={})
    user = SimpleNamespace(role="admin", tenant_id=None)
    service.create_task(req, user, Mock())
    from app.models import ModelRoute

    db.add(
        ModelRoute(template_id="fixture")
    )  # clearing overrides is semantically the legacy default
    db.commit()
    t = db.query(QuestionTemplate).one()
    t.updated_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.commit()
    with pytest.raises(DuplicateTaskError):
        service.create_task(req, user, Mock())
    t.version += 1
    db.commit()
    service.create_task(req, user, Mock())


def test_provider_cipher_and_missing_wrong_keys_fail_closed(monkeypatch):
    secret = "test-credential-not-a-live-key"
    encrypted = providers.encrypt_api_key(secret)
    assert secret not in encrypted and providers.decrypt_api_key(encrypted) == secret
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(ProviderConfigurationError):
        providers.decrypt_api_key(encrypted)
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", "")
    with pytest.raises(ProviderConfigurationError):
        providers.encrypt_api_key(secret)
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", "invalid")
    with pytest.raises(ProviderConfigurationError):
        providers.encrypt_api_key(secret)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host/v1",
        "https://user:key@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#fragment",
        "http://host:99999/v1",
        "missing-host",
    ],
)
def test_provider_base_url_validation(url):
    with pytest.raises(ValidationError):
        api.ProviderIn(name="test", base_url=url, api_key="secret")


def admin():
    return SimpleNamespace(id="admin", role="admin", tenant_id=None, status="active")


def provider(db, name="fixture-provider", base_url="http://127.0.0.1:12345/v1"):
    return api.save_provider(
        api.ProviderIn(name=name, base_url=base_url, api_key="not-a-live-key"), db, admin()
    )


def test_provider_write_only_encrypted_and_audit_redacted(db):
    saved = provider(db)
    row = db.get(ModelProvider, saved.id)
    assert "not-a-live-key" not in row.api_key_ciphertext
    assert providers.decrypt_api_key(row.api_key_ciphertext) == "not-a-live-key"
    serialized = json.dumps(saved.model_dump())
    audits = json.dumps([x.after_snapshot for x in db.query(ConfigAuditEvent).all()])
    assert (
        "not-a-live-key" not in serialized + audits
        and "api_key_ciphertext" not in serialized + audits
    )
    before = row.api_key_ciphertext
    api.save_provider(
        api.ProviderIn(id=row.id, name=row.name, base_url=row.base_url, api_key=""), db, admin()
    )
    assert row.api_key_ciphertext == before
    old_hash = row.config_hash
    api.save_provider(
        api.ProviderIn(id=row.id, name=row.name, base_url=row.base_url, api_key="new-key"),
        db,
        admin(),
    )
    assert row.config_hash != old_hash


def test_provider_update_propagates_model_identity_and_delete_is_referenced_guarded(db):
    saved = provider(db)
    profile = create_model_profile(
        ModelProfileIn(
            name="judge-custom", provider="ignored", provider_id=saved.id, model_name="test-model"
        ),
        db,
        admin(),
    )
    old_hash = profile.model_hash
    api.save_provider(
        api.ProviderIn(id=saved.id, name="renamed", base_url="http://localhost:54321/v1"),
        db,
        admin(),
    )
    db.refresh(profile)
    assert profile.provider == "renamed" and profile.model_hash != old_hash
    with pytest.raises(HTTPException) as caught:
        api.delete_provider(saved.id, db, admin())
    assert caught.value.status_code == 409
    db.delete(profile)
    db.commit()
    assert api.delete_provider(saved.id, db, admin())["deleted"]


def test_bound_client_uses_its_endpoint_and_never_global_secret(db, monkeypatch):
    saved = provider(db)
    profile = create_model_profile(
        ModelProfileIn(
            name="custom", provider="ignored", provider_id=saved.id, model_name="custom-model"
        ),
        db,
        admin(),
    )
    captured = {}
    monkeypatch.setattr(providers, "OpenAI", lambda **kw: captured.update(kw) or Mock())
    providers.client_for_profile(profile)
    assert captured["api_key"] == "not-a-live-key" and captured["base_url"].endswith(":12345/v1")
    profile.provider_record.status = "disabled"
    db.commit()
    with pytest.raises(ProviderConfigurationError):
        providers.client_for_profile(profile)


def test_route_is_persistent_and_independent_of_yaml_startup(db):
    seed(db)
    p = create_model_profile(
        ModelProfileIn(name="judge-custom", provider="environment", model_name="independent-model"),
        db,
        admin(),
    )
    api.save_route(
        api.RouteIn(template_id="fixture", generation_profile=p.name, judge_profile=p.name),
        db,
        admin(),
    )
    row = providers.judge_profile_for_template(db, "fixture", None)
    assert row.id == p.id
    from app.engine.quality import resolve_judge_model

    assert resolve_judge_model(db.query(QuestionTemplate).one(), db) == "independent-model"
    assert api.routes(db, admin())[0]["judge_profile"] == p.name
    api.save_route(api.RouteIn(template_id="fixture"), db, admin())
    assert providers.judge_profile_for_template(db, "fixture", None) is None


def test_private_or_disabled_profile_cannot_be_global_route(db):
    seed(db)
    for name, status, tenant in [("private", "enabled", "other"), ("disabled", "disabled", None)]:
        create_model_profile(
            ModelProfileIn(
                name=name, provider="env", model_name="model", status=status, tenant_id=tenant
            ),
            db,
            admin(),
        )
        with pytest.raises(HTTPException):
            api.save_route(api.RouteIn(template_id="fixture", judge_profile=name), db, admin())
    with pytest.raises(HTTPException):
        api.save_route(api.RouteIn(template_id="missing"), db, admin())


def test_model_api_admin_permission_enforced(db):
    from app.database import get_db
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    for role in ["researcher", "reviewer", "viewer"]:
        user = User(username=role, password_hash="not-used", role=role, status="active")
        db.add(user)
        db.commit()
        client = TestClient(app)
        headers = {"Authorization": "Bearer " + create_access_token(user)}
        assert client.get("/api/model-settings/providers", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/model-profiles",
                headers=headers,
                json={"name": "forbidden", "provider": "x", "model_name": "y"},
            ).status_code
            == 403
        )
    assert db.query(ModelProvider).count() == 0


def test_default_profile_switch_is_unique_for_null_scope(db):
    from app.models import ModelProfile

    first = create_model_profile(
        ModelProfileIn(name="first", provider="env", model_name="m1", is_default=True), db, admin()
    )
    second = create_model_profile(
        ModelProfileIn(name="second", provider="env", model_name="m2", is_default=True), db, admin()
    )
    db.refresh(first)
    assert not first.is_default and second.is_default
    assert db.query(ModelProfile).filter(ModelProfile.is_default.is_(True)).count() == 1
    first.is_default = True
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_provider_disabled_blocks_profile_and_active_route_disables_are_explicit(db):
    saved = provider(db)
    create_model_profile(
        ModelProfileIn(name="bound", provider="ignored", model_name="custom", provider_id=saved.id),
        db,
        admin(),
    )
    seed(db)
    api.save_route(api.RouteIn(template_id="fixture", judge_profile="bound"), db, admin())
    with pytest.raises(HTTPException) as exc:
        create_model_profile(
            ModelProfileIn(
                name="bound",
                provider="x",
                model_name="custom",
                provider_id=saved.id,
                status="disabled",
            ),
            db,
            admin(),
        )
    assert exc.value.status_code == 409
    api.save_provider(
        api.ProviderIn(id=saved.id, name=saved.name, base_url=saved.base_url, status="disabled"),
        db,
        admin(),
    )
    with pytest.raises(HTTPException):
        create_model_profile(
            ModelProfileIn(name="new", provider="x", model_name="custom", provider_id=saved.id),
            db,
            admin(),
        )


def test_old_dead_task_does_not_replay_over_competing_active_request(db):
    from app.outbox import create_generation_dispatch, replay_dead

    task = seed(db, status="failed", key="same")
    event = create_generation_dispatch(db, task)
    event.status = "dead"
    other = GenerationTask(
        template_id=task.template_id, params={}, quantity=2, status="pending", request_hash="same"
    )
    db.add(other)
    db.commit()
    with pytest.raises(ValueError, match="活动任务"):
        replay_dead(db, event.id)
    assert task.status == "failed" and event.status == "dead"


def test_null_hash_remains_compatible_with_legacy_tasks(db):
    task = seed(db)
    db.add(
        GenerationTask(
            template_id=task.template_id, params={}, quantity=1, status="pending", request_hash=None
        )
    )
    db.commit()
    assert db.query(GenerationTask).count() == 2


def test_task_api_rejects_invalid_status_and_progress(db):
    task = seed(db)
    service = GenerationService(db)
    with pytest.raises(InvalidGenerationStateError):
        service.update_task_status(task.id, "made-up")
    with pytest.raises(InvalidGenerationStateError):
        service.update_task_status(task.id, "running", progress=2)


def test_provider_conflict_not_missing_record_and_missing_credential_errors(db, monkeypatch):
    provider(db)
    for fn in (api.delete_provider, api.probe_provider):
        with pytest.raises(HTTPException) as exc:
            fn("missing", db, admin())
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        api.save_provider(
            api.ProviderIn(id="missing", name="x", base_url="http://localhost/v1"), db, admin()
        )
    with pytest.raises(HTTPException):
        api.save_provider(api.ProviderIn(name="empty", base_url="http://localhost/v1"), db, admin())
    with pytest.raises(HTTPException) as exc:
        provider(db)
    assert exc.value.status_code == 409
    monkeypatch.setattr(settings, "PROVIDER_SECRET_KEY", "")
    assert api.capabilities(admin())["credential_storage_ready"] is False


def test_models_probe_failure_is_sanitized_and_non_generation(db, monkeypatch):
    import httpx
    from openai import OpenAI

    saved = provider(db)
    secret = "echoed-private-credential"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            401, json={"error": {"message": secret, "type": "authentication_error"}}
        )
    )
    monkeypatch.setattr(
        providers,
        "OpenAI",
        lambda **kw: OpenAI(**kw, http_client=httpx.Client(transport=transport)),
    )
    with pytest.raises(HTTPException) as exc:
        api.probe_provider(saved.id, db, admin())
    assert exc.value.status_code == 502 and secret not in exc.value.detail


def test_bound_generation_sdk_error_retains_status_but_not_raw_secret(db, monkeypatch):
    import httpx
    from openai import OpenAI

    from app.engine.structured_output import generate_structured

    saved = provider(db)
    p = create_model_profile(
        ModelProfileIn(name="bound", provider="ignored", provider_id=saved.id, model_name="custom"),
        db,
        admin(),
    )
    t = SimpleNamespace(
        type_id="fixture",
        version=1,
        output_schema={
            "type": "object",
            "properties": {"stem": {"type": "string"}},
            "required": ["stem"],
        },
        run_config={"max_retry": 1},
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
    )
    secret = "echoed-private-credential"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            401, json={"error": {"message": secret, "type": "authentication_error"}}
        )
    )
    monkeypatch.setattr(
        providers,
        "OpenAI",
        lambda **kw: OpenAI(**kw, http_client=httpx.Client(transport=transport)),
    )
    trace = Mock()
    monkeypatch.setattr("app.engine.structured_output.record_trace", trace)
    with pytest.raises(HTTPException) as exc:
        generate_structured(t, {}, p)
    assert exc.value.status_code == 401 and secret not in exc.value.detail
    assert exc.value.__suppress_context__
    assert secret not in str(trace.call_args)


def test_configuration_hash_reflects_provider_binding_not_ciphertext(db):
    from app.versioning import model_profile_hash

    p = SimpleNamespace(
        name="x",
        provider="env",
        model_name="m",
        cost_tier="standard",
        is_default=False,
        status="enabled",
        max_fallbacks=1,
        budget_per_task=None,
        provider_id=None,
        provider_config_hash=None,
    )
    before = model_profile_hash(p)
    p.provider_id = "provider-id"
    p.provider_config_hash = "safe-config-hash"
    assert model_profile_hash(p) != before


def test_validation_errors_never_echo_credentials(db):
    from app.database import get_db
    from app.main import create_app
    from app.models import User
    from app.security import create_access_token

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    user = User(username="admin-test", password_hash="unused", role="admin", status="active")
    db.add(user)
    db.commit()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + create_access_token(user)}
    secret = "private-secret-do-not-echo"
    resp = client.post(
        "/api/model-settings/providers",
        headers=headers,
        json={"name": "", "base_url": "http://user:" + secret + "@localhost/v1", "api_key": secret},
    )
    assert resp.status_code == 422 and secret not in resp.text
    resp = client.post(
        "/api/model-settings/providers",
        headers=headers,
        json={"name": "x", "base_url": "http://localhost/v1", "api_key": "X" * 9000},
    )
    assert resp.status_code == 422 and "X" * 20 not in resp.text
