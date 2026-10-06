"""Non-service regressions for Compose passthrough and integration test contracts."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from tests.integration.embedding_stub import mock_vector

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/docker-compose.yml"


def test_compose_api_worker_share_all_rag_settings_and_embedding_shape():
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    required = {key for key in Settings.model_fields if key.startswith("RAG_")}
    required.update({"EMBEDDING_DIM", "EMBEDDING_TIMEOUT"})
    api = data["services"]["backend"]["environment"]
    worker = data["services"]["worker"]["environment"]
    assert required <= api.keys() and required <= worker.keys()
    for key in required:
        assert api[key] == worker[key]
        assert api[key].startswith("${" + key + ":-")


@pytest.mark.parametrize("name", ["RAG_CHUNK_TOKEN_LIMIT", "RAG_CONTEXT_TOKEN_LIMIT"])
@pytest.mark.parametrize("value", ["", "  ", None, "1024"])
def test_compose_optional_limits_accept_unset_or_valid_integer(name, value):
    settings = Settings(_env_file=None, **{name: value})
    assert getattr(settings, name) == (1024 if value == "1024" else None)


def test_compose_actual_render_keeps_api_worker_nondefault_settings(tmp_path):
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("Docker CLI required for Compose config rendering (no service start)")
    envfile = tmp_path / "empty.env"
    envfile.write_text("", encoding="utf-8")
    # These are only config-render placeholders, never written to runtime .env or deployed.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RAG_", "EMBEDDING_"))
    }
    env.update(
        {
            key: "render-test-value-not-a-secret"
            for key in [
                "POSTGRES_PASSWORD",
                "JWT_SECRET",
                "SEED_ADMIN_PASSWORD",
                "LLM_API_KEY",
                "EMBEDDING_API_KEY",
                "LANGFUSE_NEXTAUTH_SECRET",
                "LANGFUSE_SALT",
                "LANGFUSE_ENCRYPTION_KEY",
            ]
        }
    )
    env.update(
        RAG_PARENT_SIZE="4096",
        RAG_CANDIDATE_POOL="42",
        RAG_RERANK_MODE="required",
        RAG_RERANK_MODEL="render-only-model",
        RAG_QUERY_EXPANSION_MODE="llm",
        RAG_KNOWLEDGE_SCOPE="related",
        RAG_CONTEXT_TOKEN_LIMIT="",
        EMBEDDING_DIM="1024",
    )
    output = subprocess.run(
        [
            executable,
            "compose",
            "--env-file",
            str(envfile),
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    data = json.loads(output.stdout)
    for service in ["backend", "worker"]:
        values = data["services"][service]["environment"]
        assert values["RAG_PARENT_SIZE"] == "4096"
        assert values["RAG_CANDIDATE_POOL"] == "42"
        assert values["RAG_RERANK_MODE"] == "required"
        assert values["RAG_RERANK_MODEL"] == "render-only-model"
        assert values["RAG_KNOWLEDGE_SCOPE"] == "related"
        assert values["RAG_QUERY_EXPANSION_MODE"] == "llm"
        assert values["RAG_CONTEXT_TOKEN_LIMIT"] == ""
        # Exercise empty optional-limit handling with the actual rendered map.
        typed = Settings(
            _env_file=None,
            **{
                key: value
                for key, value in values.items()
                if key.startswith("RAG_") or key in {"EMBEDDING_DIM", "EMBEDDING_TIMEOUT"}
            },
            ENVIRONMENT="development",
        )
        assert typed.RAG_CONTEXT_TOKEN_LIMIT is None


@pytest.mark.parametrize("content", ["", "present simple", "walking verbs", "中文表格", "123 -ed"])
def test_embedding_stub_has_normalized_finite_nonzero_shape(content):
    vector = mock_vector(content)
    assert len(vector) == 1024 and all(math.isfinite(v) for v in vector)
    assert any(vector) and sum(v * v for v in vector) == pytest.approx(1.0)
    assert vector == mock_vector(content)


def test_embedding_stub_varies_by_content_with_reproducible_query_similarity():
    query = mock_vector("agreement rules")
    match = mock_vector("agreement rules subject")
    unrelated = mock_vector("volcano purple ocean")
    assert match != unrelated
    assert sum(a * b for a, b in zip(query, match)) > sum(a * b for a, b in zip(query, unrelated))


def test_integration_setup_requires_real_migrations_and_never_truncates_shared_db():
    fixture = (ROOT / "backend/tests/integration/conftest.py").read_text(encoding="utf-8")
    pipeline = (ROOT / "backend/tests/integration/test_pipeline.py").read_text(encoding="utf-8")
    recovery = (ROOT / "backend/tests/integration/test_persistent_recovery.py").read_text(
        encoding="utf-8"
    )
    assert '"-m", "alembic", "upgrade", "head"' in fixture
    assert "create_all(" not in fixture + recovery
    assert "TRUNCATE TABLE" not in fixture + pipeline
    assert "mock_vector(value, settings.EMBEDDING_DIM)" in fixture
    assert 'join_transaction_mode="create_savepoint"' in fixture


@pytest.mark.parametrize("actual_head", ["opt079_public_merge", "outdated-head"])
def test_integration_fixture_checks_database_revision_after_migration(monkeypatch, actual_head):
    from types import SimpleNamespace

    import sqlalchemy

    from tests.integration import conftest as integration

    commands = []
    monkeypatch.setattr(
        integration, "TEST_DATABASE_URL", "postgresql+psycopg2://test:test@invalid/test"
    )
    monkeypatch.setattr(
        integration.subprocess,
        "run",
        lambda args, **kwargs: (
            commands.append(args) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    disposed = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return [self.value]

        def scalar(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            return Result(actual_head if "alembic_version" in str(statement) else "mock-extension")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *_: Engine())
    if actual_head == "opt079_public_merge":
        assert integration._pg_ready.__wrapped__() is True
    else:
        with pytest.raises(AssertionError, match="head"):
            integration._pg_ready.__wrapped__()
    assert commands[0][-4:] == ["-m", "alembic", "upgrade", "head"]
    assert disposed == [True]


def test_integration_fixture_failed_migration_does_not_stamp_or_open_another_database(monkeypatch):
    from types import SimpleNamespace

    import sqlalchemy

    from tests.integration import conftest as integration

    monkeypatch.setattr(
        integration, "TEST_DATABASE_URL", "postgresql+psycopg2://test:test@invalid/test"
    )
    monkeypatch.setattr(
        integration.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="password=must-not-be-exposed", stdout=""
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Migration failure must stop fixture setup")

    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    with pytest.raises(pytest.fail.Exception, match="Alembic") as failure:
        integration._pg_ready.__wrapped__()
    assert "must-not-be-exposed" not in str(failure.value)
