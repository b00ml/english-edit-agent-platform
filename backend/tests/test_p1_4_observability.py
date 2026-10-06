from types import SimpleNamespace

import pytest

from app.config import settings
from app.engine.trace import sanitize_trace_data
from app.health import readiness_report
from app.rag import embedding
from app.rag.retriever import retrieve


def test_sanitize_trace_data_redacts_nested_and_inline_credentials():
    data = {
        "config": {"llm_api_key": "secret-value", "nested": [{"password": "pw"}]},
        "headers": "Authorization: Bearer abc123 token=xyz",
        "body": "api_key=inline-secret; normal=text",
    }

    result = sanitize_trace_data(data)

    assert result["config"]["llm_api_key"] == "[REDACTED]"
    assert result["config"]["nested"][0]["password"] == "[REDACTED]"
    assert "abc123" not in result["headers"]
    assert "xyz" not in result["headers"]
    assert "inline-secret" not in result["body"]
    assert "normal=text" in result["body"]


def test_embedding_records_success_trace(monkeypatch):
    traced = []

    class _Embeddings:
        def create(self, **kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.1, 0.2])],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=0),
            )

    monkeypatch.setattr(
        embedding, "_get_openai_client", lambda: SimpleNamespace(embeddings=_Embeddings())
    )
    monkeypatch.setattr(embedding, "record_trace", lambda **kwargs: traced.append(kwargs))

    vectors = embedding.embed_texts(
        ["query"],
        trace_id="trace-1",
        task_id="task-1",
        template_id="single_choice",
        tenant_id="tenant-1",
    )

    assert vectors == [[0.1, 0.2]]
    assert len(traced) == 1
    assert traced[0]["trace_id"] == "trace-1"
    assert traced[0]["stage"] == "embedding"
    assert traced[0]["prompt_tokens"] == 12
    assert traced[0]["output_data"]["dimension"] == 2
    assert traced[0]["cost"] >= 0


def test_embedding_records_failure_trace_and_reraises(monkeypatch):
    traced = []

    class _Embeddings:
        def create(self, **kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        embedding, "_get_openai_client", lambda: SimpleNamespace(embeddings=_Embeddings())
    )
    monkeypatch.setattr(embedding, "record_trace", lambda **kwargs: traced.append(kwargs))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        embedding.embed_texts(["query"], trace_id="trace-fail")

    assert len(traced) == 1
    assert traced[0]["success"] is False
    assert traced[0]["stage"] == "embedding"
    assert traced[0]["output_data"]["error_summary"] == "provider unavailable"


def test_retriever_passes_trace_context_to_embedding(monkeypatch):
    captured = {}

    def _embed(texts, **kwargs):
        captured.update(kwargs)
        return [[0.1, 0.2]]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        def execute(self, statement):
            return _Result()

    monkeypatch.setattr("app.rag.retriever.embed_texts", _embed)

    assert (
        retrieve(
            _Session(),
            "query",
            tenant_id="tenant-1",
            trace_id="trace-1",
            task_id="task-1",
            template_id="single_choice",
        )
        == []
    )
    assert captured == {
        "trace_id": "trace-1",
        "task_id": "task-1",
        "template_id": "single_choice",
        "tenant_id": "tenant-1",
    }


def test_readiness_blocks_degraded_checkpointer_in_production(monkeypatch):
    dependencies = [
        {"name": "database", "status": "healthy", "latency_ms": 1, "detail": "ok"},
        {"name": "alembic", "status": "healthy", "latency_ms": 1, "detail": "v1"},
        {"name": "redis", "status": "healthy", "latency_ms": 1, "detail": "ok"},
        {"name": "checkpointer", "status": "degraded", "latency_ms": 1, "detail": "MemorySaver"},
    ]
    monkeypatch.setattr(
        "app.health.dependency_report", lambda: {"status": "degraded", "dependencies": dependencies}
    )
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_MEMORY_CHECKPOINTER", False)

    report = readiness_report()

    assert report["ready"] is False
    assert report["blocking"] == ["checkpointer"]


def test_readiness_allows_degraded_checkpointer_when_explicitly_enabled(monkeypatch):
    dependencies = [
        {"name": "database", "status": "healthy", "latency_ms": 1, "detail": "ok"},
        {"name": "alembic", "status": "healthy", "latency_ms": 1, "detail": "v1"},
        {"name": "redis", "status": "healthy", "latency_ms": 1, "detail": "ok"},
        {"name": "checkpointer", "status": "degraded", "latency_ms": 1, "detail": "MemorySaver"},
    ]
    monkeypatch.setattr(
        "app.health.dependency_report", lambda: {"status": "degraded", "dependencies": dependencies}
    )
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_MEMORY_CHECKPOINTER", True)

    report = readiness_report()

    assert report["ready"] is True
    assert report["blocking"] == []


def test_get_trace_preserves_persisted_lifecycle_stage(monkeypatch):
    """生命周期 Trace 不应被旧版 generate/qc 推断逻辑覆盖。"""
    from app.api import routes

    rows = [
        SimpleNamespace(stage="queue_item", output_data={"event": "started"}),
        SimpleNamespace(stage=None, output_data={"dimension_scores": {"accuracy": 0.8}}),
    ]

    class _Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, *_args):
            return _Query()

    monkeypatch.setattr(
        routes.TraceOut,
        "model_validate",
        classmethod(lambda cls, row: SimpleNamespace(stage=row.stage or "generate")),
    )

    result = routes.get_trace(
        "trace-1", db=_Db(), current_user=SimpleNamespace(role="admin", tenant_id=None)
    )

    assert [item.stage for item in result] == ["queue_item", "qc"]
