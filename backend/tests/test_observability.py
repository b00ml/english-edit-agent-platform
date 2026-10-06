# tests/test_observability.py —— Trace Sink 分发单测（P0-5 / OPT-017）
import sys
import types

import pytest

from app.config import settings
from app.engine import observability
from app.engine.observability import LangfuseSink, TraceLogSink
from app.engine.trace import record_trace, reset_sinks


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = 0
        self.closed = 0

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _reset_sinks():
    reset_sinks()
    yield
    reset_sinks()


class TestTraceLogSink:
    def test_emit_writes_row_with_attempt_success(self, monkeypatch):
        fake = _FakeSession()
        monkeypatch.setattr("app.database.SessionLocal", lambda: fake)
        TraceLogSink().emit(
            {
                "trace_id": "t1",
                "model": "m",
                "latency_ms": 1.0,
                "cost": 0.1,
                "stage": "generate",
                "attempt": 2,
                "success": False,
                "output_data": {"error_summary": "x"},
            }
        )
        assert fake.committed == 1 and fake.closed == 1
        row = fake.added[0]
        assert row.trace_id == "t1"
        assert row.attempt == 2
        assert row.success is False
        assert row.output_data == {"error_summary": "x"}

    def test_emit_failure_raises_for_caller_to_catch(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("app.database.SessionLocal", _boom)
        with pytest.raises(RuntimeError):
            TraceLogSink().emit({"trace_id": "t1"})


class TestSinkDispatch:
    def test_record_trace_dispatches_to_configured_sinks(self, monkeypatch):
        calls = []

        class _SinkA:
            def emit(self, t):
                calls.append(("a", t))

        class _SinkB:
            def emit(self, t):
                calls.append(("b", t))

        monkeypatch.setattr(observability, "TraceLogSink", _SinkA)
        monkeypatch.setattr(observability, "LangfuseSink", _SinkB)
        monkeypatch.setattr(settings, "TRACE_SINKS", "db,langfuse")
        record_trace(trace_id="t1", model="m", latency_ms=1.0, cost=0.0)
        assert [name for name, _ in calls] == ["a", "b"]
        # attempt/success 默认值随 trace dict 分发
        assert calls[0][1]["attempt"] == 1 and calls[0][1]["success"] is True

    def test_one_sink_failure_does_not_block_other(self, monkeypatch):
        called = []

        class _BadSink:
            def emit(self, t):
                raise RuntimeError("boom")

        class _GoodSink:
            def emit(self, t):
                called.append(t)

        monkeypatch.setattr(observability, "TraceLogSink", _BadSink)
        monkeypatch.setattr(observability, "LangfuseSink", _GoodSink)
        monkeypatch.setattr(settings, "TRACE_SINKS", "db,langfuse")
        # 第一个 Sink 抛错不中断第二个 Sink，也不向外抛
        record_trace(trace_id="t1", model="m", latency_ms=1.0, cost=0.0)
        assert len(called) == 1

    def test_unknown_sink_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINKS", "foobar")
        # 未知 sink 跳过且不报错（此时无任何 sink 被调用）
        record_trace(trace_id="t1", model="m", latency_ms=1.0, cost=0.0)


class TestLangfuseSink:
    def test_noop_without_keys(self, monkeypatch):
        monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
        sink = LangfuseSink()
        with pytest.raises(RuntimeError, match="not been configured"):
            sink.emit({"trace_id": "t1"})  # 未配置不得伪装导出成功；无网络请求
        assert sink._get_client() is None

    def test_emit_with_client_builds_generation(self, monkeypatch):
        events = []

        class _Trace:
            def __init__(self, **kw):
                events.append(("trace", kw))

            def generation(self, **kw):
                events.append(("generation", kw))

        class _LF:
            def __init__(self, **kw):
                events.append(("init", kw))

            def trace(self, **kw):
                return _Trace(**kw)

        fake_mod = types.ModuleType("langfuse")
        fake_mod.Langfuse = _LF
        monkeypatch.setitem(sys.modules, "langfuse", fake_mod)
        monkeypatch.setattr(settings, "LANGFUSE_HOST", "http://x")
        monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk")

        LangfuseSink().emit(
            {
                "trace_id": "t1",
                "stage": "generate",
                "model": "m",
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "latency_ms": 100.0,
                "attempt": 2,
                "success": False,
            }
        )
        kinds = [k for k, _ in events]
        assert "init" in kinds and "trace" in kinds and "generation" in kinds
        gen_kw = dict(events[-1][1])
        assert gen_kw["model"] == "m"
        assert gen_kw["usage"] == {"input": 3, "output": 4, "unit": "TOKENS"}
        assert gen_kw["latency"] == 0.1
