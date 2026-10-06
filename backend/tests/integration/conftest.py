# tests/integration/conftest.py —— 集成测试环境装配（P1-4 / OPT-025）
# 集成测试需要真实 Postgres（模型使用 JSONB + pgvector，sqlite 不可行）：
#   1. 本地：docker run pgvector/pgvector:pg16 后设置 TEST_DATABASE_URL 再运行
#      `pytest tests/integration`；
#   2. CI：backend-ci.yml integration job 注入 service 容器与 TEST_DATABASE_URL。
# 未设置 TEST_DATABASE_URL 时整个目录被跳过（默认单测流程不受影响）。
# 可复用已有 PostgreSQL：先执行 Alembic，再用事务或 UUID 清理测试自己的数据；不清库。
# app.config 可能已被纯单测导入；pg_engine/pg_app_sessions 显式绑定 TEST_DATABASE_URL，
# 因此完整 suite 和单独 integration 运行都必须验证真正的数据库 head。
import os
import subprocess
import sys
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

if TEST_DATABASE_URL:
    # 必须在 app.config 首次导入前生效
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    # API pipeline 使用进程内 saver；独立进程持久化由 test_persistent_recovery 真 PG 验证
    os.environ.setdefault("CHECKPOINTER_BACKEND", "memory")
    # 测试专用 JWT 密钥（避免弱密钥告警干扰）
    os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret")


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="需要 TEST_DATABASE_URL（真实 Postgres）")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def _pg_ready():
    """Run actual Alembic migrations, not ORM create_all or an assumed stamp."""
    from sqlalchemy import create_engine, text

    if not TEST_DATABASE_URL:
        pytest.skip("需要 TEST_DATABASE_URL（真实 Postgres）")
    root = Path(__file__).resolve().parents[2]
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "ENVIRONMENT": "development"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if migrated.returncode:
        # Provider credentials/connection strings must not leak via exception payloads.
        pytest.fail(
            "真实 Alembic upgrade head 失败；请检查现有数据库迁移版本，不自动 stamp 或删除数据"
        )
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            actual = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
            assert actual == expected, "数据库实际迁移版本必须等于代码 head"
            assert connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            ).scalar()
    finally:
        engine.dispose()
    return True


@pytest.fixture(autouse=True)
def mock_embedding(monkeypatch):
    """Hash test vectors are normalized, non-zero and vary by input; never call a cloud API."""
    from types import SimpleNamespace

    from app.config import settings
    from app.rag import embedding as embedding_module
    from tests.integration.embedding_stub import mock_vector

    class _FakeEmbeddingClient:
        @property
        def embeddings(self):
            return self

        def create(self, model=None, input=None, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=i, embedding=mock_vector(value, settings.EMBEDDING_DIM))
                    for i, value in enumerate(input or [])
                ]
            )

    monkeypatch.setattr(embedding_module, "_get_openai_client", lambda: _FakeEmbeddingClient())


@pytest.fixture(autouse=True)
def unpaid_rag_providers(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "aliases")


@pytest.fixture(scope="session")
def pg_engine(_pg_ready):
    from sqlalchemy import create_engine

    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session(pg_engine):
    """Reuse the existing DB, roll back only this test's transaction (no TRUNCATE)."""
    from sqlalchemy.orm import Session

    with pg_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture()
def pg_app_sessions(pg_engine, monkeypatch, request):
    """Joined savepoint sessions let API/worker/Trace commits share a rollback envelope."""
    from sqlalchemy.orm import sessionmaker

    from app import database, main
    from app.config import settings
    from app.engine import trace
    from app.worker import tasks
    from app.workflow import graph

    with pg_engine.connect() as connection:
        transaction = connection.begin()
        factory = sessionmaker(
            bind=connection, join_transaction_mode="create_savepoint", autoflush=False
        )
        for module in [database, main, tasks, graph, request.module]:
            if hasattr(module, "SessionLocal"):
                monkeypatch.setattr(module, "SessionLocal", factory)
        monkeypatch.setattr(settings, "DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
        monkeypatch.setattr(settings, "ALLOW_MEMORY_CHECKPOINTER", True)
        monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
        monkeypatch.setattr(settings, "TRACE_SINKS", "db")
        monkeypatch.setattr(settings, "JWT_SECRET", "test-secret-test-secret-test-secret")
        graph.reset_checkpointer()
        trace.reset_sinks()
        try:
            yield factory
        finally:
            graph.reset_checkpointer()
            trace.reset_sinks()
            transaction.rollback()


@pytest.fixture()
def client(pg_app_sessions, monkeypatch):
    """Startup requires migrated head; staging avoids create_all masking migration gaps."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client, pg_app_sessions):
    import uuid

    from app.models import User
    from app.security import hash_password

    username = f"integration-{uuid.uuid4().hex[:16]}"
    password = "only-for-this-test-transaction"
    with pg_app_sessions() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                status="active",
            )
        )
        session.commit()
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def inline_celery(monkeypatch):
    """Execute worker bodies in-process; never send fixture jobs to a real broker."""
    from app.api import routes as routes_module
    from app.worker import tasks

    # shared_task is a current-app proxy; patching proxy.delay can target the
    # wrong Task when Celery changes current app during .apply(). Pin bodies and
    # replace the dispatch module's producer, not a transient proxy attribute.
    dispatch = tasks.dispatch_generation_items._get_current_object()
    generate = tasks.generate_single_item._get_current_object()

    class SimpleAsyncResult:
        def __init__(self, value):
            self._value = value

        def get(self, timeout=None):
            return self._value

    def _fake_send_task(name, args=None, **kwargs):
        if name == "app.worker.tasks.process_generation_task":
            return SimpleAsyncResult(dispatch.run(*(args or [])))
        if name == "app.worker.tasks.generate_single_item":
            return SimpleAsyncResult(generate.run(*(args or [])))
        raise AssertionError("Unexpected task in offline pipeline")

    def _fake_item_delay(*args, **kwargs):
        return SimpleAsyncResult(generate.run(*args, **kwargs))

    monkeypatch.setattr(
        tasks,
        "_item_sender",
        lambda name, args, task_id=None: _fake_send_task(name, args, task_id=task_id),
    )
    monkeypatch.setattr(routes_module.celery_app, "send_task", _fake_send_task)
    return _fake_send_task


class _FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50


class FakeLLMClient:
    """脚本化 OpenAI 兼容客户端：按调用顺序返回内容或抛错。"""

    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0)
        from types import SimpleNamespace

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=_FakeUsage(),
        )


@pytest.fixture()
def mock_llm(monkeypatch):
    """替换生成与质检两个引擎的 LLM 客户端；返回 (gen_client, judge_client)。"""
    from app.engine import quality as quality_module
    from app.engine import structured_output as so_module

    gen_client = FakeLLMClient([])
    judge_client = FakeLLMClient([])
    monkeypatch.setattr(so_module, "_get_openai_client", lambda: gen_client)
    monkeypatch.setattr(quality_module, "_get_openai_client", lambda: judge_client)
    return gen_client, judge_client


GEN_OK = (
    '{"stem": "Choose the correct form: She ___ to school every day.", '
    '"options": ["go", "goes", "going", "gone"], "answer": "B", '
    '"explanation": "一般现在时第三人称单数用 goes。"}'
)
JUDGE_HIGH = (
    '{"dimension_scores": {"kp_match": 90, "diff_match": 85, '
    '"distractor": 88, "unambiguous": 92}}'
)
JUDGE_GRAY = (
    '{"dimension_scores": {"kp_match": 65, "diff_match": 65, '
    '"distractor": 65, "unambiguous": 65}}'
)
