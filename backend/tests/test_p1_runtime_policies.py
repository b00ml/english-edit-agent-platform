import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError

from app.config import Settings, settings
from app.errors import ModelRoutingError, QualityCheckError
from app.main import _bootstrap, _seed_model_profiles, create_app, lifespan
from app.models import GenerationTask, GenerationTaskItem, ModelProfile
from app.versioning import model_profile_hash
from app.worker import tasks
from app.worker.failure_policy import classify_failure


@pytest.mark.parametrize(
    "status,retryable",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (422, False),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_failure_policy_uses_status_code_not_text(status, retryable):
    error = APIStatusError(
        "opaque message with misleading 422/429 digits",
        response=httpx.Response(status, request=httpx.Request("POST", "https://test.invalid")),
        body=None,
    )
    assert classify_failure(error).retryable is retryable
    assert classify_failure(ModelRoutingError("wrapped", error)).retryable is retryable


def test_unknown_message_digits_never_imply_retry_or_permanence():
    assert classify_failure(RuntimeError("429 500 422 400")).code == "UNKNOWN_ERROR"
    assert not classify_failure(RuntimeError("429 500 422 400")).retryable
    assert classify_failure(QualityCheckError("invalid output")).code == "QUALITY_CHECK_FAILED"


def test_connection_failure_is_transient_without_embedded_status():
    error = APIConnectionError(request=httpx.Request("POST", "https://test.invalid"))
    assert classify_failure(error).retryable


def test_transient_exhaustion_writes_failure_and_final_parent(db, monkeypatch):
    from test_p0_quality_events import seed

    state = seed(db)
    db.add(
        GenerationTaskItem(
            task_id=state["task_id"], item_index=0, thread_id=state["trace_id"], tenant_id=None
        )
    )
    db.commit()

    def failed(*args, **kwargs):
        raise HTTPException(status_code=503, detail="secret provider text")

    monkeypatch.setattr(tasks, "run_generation", failed)
    result = tasks.generate_single_item.apply(
        args=[state["task_id"], 0], retries=tasks.generate_single_item.max_retries
    ).get()
    assert result["failure_code"] == "RETRY_EXHAUSTED"
    assert db.query(GenerationTask).one().status == "failed"
    assert db.query(GenerationTaskItem).one().status == "failed"
    assert "secret" not in result["reason"]


def test_seeded_tiers_use_explicit_mapping_and_hash_actual_fields(db, monkeypatch):
    monkeypatch.setattr(
        settings,
        "MODEL_PROFILE_MODELS",
        {"lite": "m-lite", "standard": "m-standard", "high": "m-high"},
    )
    _seed_model_profiles(db)
    profiles = db.query(ModelProfile).all()
    assert {p.model_name for p in profiles} == {"m-lite", "m-standard", "m-high"}
    assert all(p.model_hash == model_profile_hash(p) for p in profiles)
    profile = next(p for p in profiles if p.name == "lite")
    profile.model_name = "admin-edited"
    db.commit()
    _seed_model_profiles(db)
    assert profile.model_name == "admin-edited" and profile.model_hash == model_profile_hash(
        profile
    )


def test_real_yaml_difficulty_enum_routes_hard_tier():
    from pathlib import Path

    import yaml

    from app.engine.router import _resolve_primary_profile_name

    for path in (Path(__file__).parents[1] / "app" / "templates").glob("*.yaml"):
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert (
            _resolve_primary_profile_name(config["run_config"], {"difficulty": "难"})
            == config["run_config"]["model_profile"]["难"]
        )


def test_bootstrap_rejects_partial_template_load(db, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr("app.main.load_all_templates", lambda *_args: 0)
    with pytest.raises(RuntimeError, match="完整加载"):
        _bootstrap(db)


def test_critical_startup_failure_propagates_and_rolls_back(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr("app.main.SessionLocal", lambda: fake)

    def bad(*_args):
        raise RuntimeError("critical init failed")

    monkeypatch.setattr("app.main._bootstrap", bad)
    create = Mock()
    monkeypatch.setattr("app.main.Base.metadata.create_all", create)

    async def run():
        async with lifespan(create_app()):
            raise AssertionError("must not serve")

    with pytest.raises(RuntimeError, match="critical init"):
        asyncio.run(run())
    fake.rollback.assert_called_once()
    fake.close.assert_called_once()
    create.assert_not_called()


def test_required_independent_tiers_and_judge_fail_when_mappings_equal(db, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr("app.main.load_all_templates", lambda *_args: 3)
    monkeypatch.setattr("app.main.validate_template_model_references", lambda *_args: [])
    monkeypatch.setattr(settings, "MODEL_PROFILE_MODELS", {})
    monkeypatch.setattr(settings, "REQUIRE_DISTINCT_MODEL_TIERS", True)
    with pytest.raises(RuntimeError, match="分档"):
        _bootstrap(db)
    monkeypatch.setattr(settings, "REQUIRE_DISTINCT_MODEL_TIERS", False)
    monkeypatch.setattr(settings, "REQUIRE_INDEPENDENT_JUDGE", True)
    monkeypatch.setattr(settings, "JUDGE_MODEL_NAME", "")
    with pytest.raises(RuntimeError, match="独立 Judge"):
        _bootstrap(db)


def test_cors_allowlist_rejects_untrusted_preflight(monkeypatch):
    monkeypatch.setattr(settings, "CORS_ALLOWED_ORIGINS", ["https://trusted.example"])
    monkeypatch.setattr(settings, "CORS_ALLOW_CREDENTIALS", False)
    client = TestClient(create_app())
    headers = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    denied = client.options("/api/generate", headers=headers)
    assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers
    headers["Origin"] = "https://trusted.example"
    allowed = client.options("/api/generate", headers=headers)
    assert (
        allowed.status_code == 200
        and allowed.headers["access-control-allow-origin"] == "https://trusted.example"
    )
    assert "access-control-allow-credentials" not in allowed.headers


def test_config_rejects_wildcard_credentials_and_accepts_blank_profile_json(monkeypatch):
    with pytest.raises(ValueError, match="CORS"):
        Settings(
            _env_file=None,
            ENVIRONMENT="development",
            CORS_ALLOWED_ORIGINS=["*"],
            CORS_ALLOW_CREDENTIALS=True,
        )
    monkeypatch.setenv("MODEL_PROFILE_MODELS", "")
    assert Settings(_env_file=None, ENVIRONMENT="development").MODEL_PROFILE_MODELS == {}


def test_judge_endpoint_can_be_configured_independently(monkeypatch):
    from app.engine import quality

    captured = {}
    monkeypatch.setattr(settings, "JUDGE_API_BASE", "https://judge.test.invalid/v1")
    monkeypatch.setattr(settings, "JUDGE_API_KEY", "test-only-key")
    monkeypatch.setattr(quality, "OpenAI", lambda **kwargs: captured.update(kwargs))
    quality._get_openai_client()
    assert (
        captured["base_url"] == settings.JUDGE_API_BASE
        and captured["api_key"] == settings.JUDGE_API_KEY
    )
