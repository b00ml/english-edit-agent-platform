# tests/test_structured_output.py —— 结构化输出纯逻辑单测
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from app.engine.structured_output import (
    _build_pydantic_from_schema,
    _build_schema_instruction,
    _map_schema_type,
    _normalize_flat,
)


class TestMapSchemaType:
    def test_string_default(self):
        assert _map_schema_type({"type": "string"}) is str

    def test_string_with_enum(self):
        t = _map_schema_type({"type": "string", "enum": ["A", "B"]})
        # 返回 Literal 类型，验证其允许的取值集合
        assert t is not str
        assert Literal["A", "B"] == t

    def test_integer_number_boolean(self):
        assert _map_schema_type({"type": "integer"}) is int
        assert _map_schema_type({"type": "number"}) is float
        assert _map_schema_type({"type": "boolean"}) is bool

    def test_array_with_object_items_returns_list_model(self):
        items = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        t = _map_schema_type({"type": "array", "items": items})
        # 返回 List[InnerModel] 泛型别名，其元素类型是 Pydantic 模型
        assert getattr(t, "__origin__", None) is list
        assert getattr(t, "__args__", (None,))[0].__name__ == "OutputModel"

    def test_unknown_type_returns_any(self):
        assert _map_schema_type({"type": "file"}) is Any


class TestNormalizeFlat:
    SCHEMA = {"type": "object", "required": ["stem", "answer"]}

    def test_valid_data_unchanged(self):
        data = {"stem": "x", "answer": "A"}
        assert _normalize_flat(self.SCHEMA, data) == data

    def test_nested_wrapper_unwrapped(self):
        data = {"result": {"stem": "x", "answer": "A"}}
        assert _normalize_flat(self.SCHEMA, data) == {"stem": "x", "answer": "A"}

    def test_missing_no_nested_returns_unchanged(self):
        data = {"stem": "x"}
        assert _normalize_flat(self.SCHEMA, data) == data

    def test_list_input_returned_unchanged_without_error(self):
        # 模型偶发返回 JSON 数组（list）时，原样返回交由 Pydantic 校验判错并触发重试，
        # 不得对 list 调用 .keys() 抛 AttributeError（回归：'list' object has no attribute 'keys'）
        data = [{"stem": "x", "answer": "A"}]
        assert _normalize_flat(self.SCHEMA, data) is data

    def test_array_wrapper_unwrapped(self):
        # 模型偶发返回 {"questions":[{...}]} 数组包装时，取首个满足必填字段的子对象，
        # 减少无谓重试导致的失败/耗时（回归：item 4 因未解包而 3 次重试失败）
        data = {"questions": [{"stem": "x", "answer": "A"}]}
        assert _normalize_flat(self.SCHEMA, data) == {"stem": "x", "answer": "A"}

    def test_array_wrapper_empty_or_invalid_returns_unchanged(self):
        # 数组包装内无满足必填字段的对象时，原样返回交由 Pydantic 判错
        data = {"questions": [{"stem": "x"}]}
        assert _normalize_flat(self.SCHEMA, data) is data


class TestBuildPydanticFromSchema:
    SCHEMA = {
        "type": "object",
        "required": ["stem", "options", "answer"],
        "properties": {
            "stem": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
        },
    }

    def test_valid_payload_validates(self):
        model = _build_pydantic_from_schema(self.SCHEMA)
        out = model.model_validate({"stem": "s", "options": ["a", "b", "c", "d"], "answer": "A"})
        assert out.answer == "A"

    def test_invalid_payload_raises(self):
        model = _build_pydantic_from_schema(self.SCHEMA)
        with pytest.raises(ValidationError):
            model.model_validate({"stem": "s", "answer": "E"})

    def test_top_level_array_uses_wrapper(self):
        model = _build_pydantic_from_schema({"type": "array", "items": {"type": "string"}})
        out = model.model_validate({"items": ["a", "b"]})
        assert out.items == ["a", "b"]


class TestBuildSchemaInstruction:
    def test_top_level_object_includes_required_marker(self):
        schema = {
            "type": "object",
            "required": ["stem"],
            "properties": {"stem": {"type": "string"}},
        }
        text = _build_schema_instruction(schema)
        assert "stem" in text
        assert "必填" in text

    def test_enum_render(self):
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string", "enum": ["A", "B"]}},
        }
        text = _build_schema_instruction(schema)
        assert "A" in text and "B" in text

    def test_non_object_returns_empty(self):
        assert _build_schema_instruction({"type": "array"}) == ""


# ---------------------------------------------------------------------------
# P0-1 / OPT-015：校验错误回注重试（generate_structured 集成级行为，mock LLM 客户端）
# ---------------------------------------------------------------------------
import json
from types import SimpleNamespace

from app.config import settings
from app.engine.structured_output import (
    _summarize_validation_error,
    generate_structured,
)
from app.errors import StructuredOutputError


class _FakeResponse:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)


class _FakeCompletions:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._contents.pop(0))


class _FakeClient:
    def __init__(self, contents):
        self.chat = SimpleNamespace(completions=_FakeCompletions(contents))


def _template(max_retry=3):
    return SimpleNamespace(
        type_id="single_choice",
        version=1,
        run_config={"max_retry": max_retry},
        output_schema={
            "type": "object",
            "required": ["stem", "answer"],
            "properties": {
                "stem": {"type": "string"},
                "answer": {"type": "string", "enum": ["A", "B", "C", "D"]},
            },
        },
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
    )


def _run_generate(monkeypatch, contents, max_retry=3):
    """跑一次 generate_structured：mock OpenAI 客户端与 record_trace，返回 (调用记录, trace 记录, 结果/异常)。"""
    client = _FakeClient(contents)
    monkeypatch.setattr("app.engine.structured_output.OpenAI", lambda **kw: client)
    traced = []
    monkeypatch.setattr("app.engine.structured_output.record_trace", lambda **kw: traced.append(kw))
    outcome = {"value": None, "error": None}
    try:
        outcome["value"] = generate_structured(
            _template(max_retry=max_retry), {"knowledge_point": "一般现在时"}, None, trace_id="t1"
        )
    except StructuredOutputError as exc:
        outcome["error"] = exc
    return client.chat.completions.calls, traced, outcome


class TestSummarizeValidationError:
    def test_validation_error_lists_loc_type_msg(self):
        schema = {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "enum": ["A", "B"]}},
        }
        model = _build_pydantic_from_schema(schema)
        with pytest.raises(ValidationError) as exc_info:
            model.model_validate({"answer": "Z"})
        summary = _summarize_validation_error(exc_info.value)
        assert "answer" in summary
        assert "Input should be" in summary

    def test_input_truncated_to_80_chars(self):
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        model = _build_pydantic_from_schema(schema)
        with pytest.raises(ValidationError) as exc_info:
            model.model_validate({"a": "x" * 200})
        summary = _summarize_validation_error(exc_info.value)
        assert "..." in summary
        assert "x" * 100 not in summary

    def test_json_decode_error_message(self):
        summary = _summarize_validation_error(json.JSONDecodeError("Expecting value", "doc", 0))
        assert "不是合法 JSON" in summary

    def test_key_error_message(self):
        assert "缺少预期字段" in _summarize_validation_error(KeyError("answer"))


class TestGenerateStructuredRetry:
    def test_first_attempt_success_no_feedback(self, monkeypatch):
        calls, traced, outcome = _run_generate(monkeypatch, ['{"stem": "s", "answer": "A"}'])
        assert outcome["error"] is None
        assert outcome["value"] == {"stem": "s", "answer": "A"}
        assert len(calls) == 1
        assert len(calls[0]["messages"]) == 2
        # 成功行 attempt=1 / success=True
        assert traced[0]["attempt"] == 1 and traced[0]["success"] is True

    def test_validation_error_feedback_injected(self, monkeypatch):
        calls, traced, outcome = _run_generate(
            monkeypatch, ['{"stem": "s", "answer": "Z"}', '{"stem": "s", "answer": "A"}']
        )
        assert outcome["value"] == {"stem": "s", "answer": "A"}
        assert len(calls) == 2
        second = calls[1]["messages"]
        # 回注后：system, user, assistant(上次输出), user(错误反馈)
        assert len(second) == 4
        assert second[2]["role"] == "assistant" and '"answer": "Z"' in second[2]["content"]
        assert second[3]["role"] == "user"
        assert "未通过结构校验" in second[3]["content"]
        assert "answer" in second[3]["content"]

    def test_json_decode_error_retry_without_assistant_echo(self, monkeypatch):
        calls, traced, outcome = _run_generate(
            monkeypatch, ['{"stem": ', '{"stem": "s", "answer": "A"}']
        )
        assert outcome["value"] == {"stem": "s", "answer": "A"}
        second = calls[1]["messages"]
        # JSONDecodeError 无可用结构化输出：只追加错误说明，不追加 assistant 回显
        assert len(second) == 3
        assert all(m["role"] != "assistant" for m in second)

    def test_retry_exhausted_raises_with_summaries(self, monkeypatch):
        bad = '{"stem": "s", "answer": "Z"}'
        calls, traced, outcome = _run_generate(monkeypatch, [bad, bad, bad])
        assert outcome["value"] is None
        assert isinstance(outcome["error"], StructuredOutputError)
        assert "第1次" in str(outcome["error"]) and "第3次" in str(outcome["error"])
        assert len(calls) == 3

    def test_feedback_disabled_resends_same_messages(self, monkeypatch):
        monkeypatch.setattr(settings, "STRUCTURED_RETRY_FEEDBACK", False)
        calls, traced, outcome = _run_generate(
            monkeypatch, ['{"stem": "s", "answer": "Z"}', '{"stem": "s", "answer": "A"}']
        )
        assert outcome["value"] == {"stem": "s", "answer": "A"}
        # 关闭回注：两次请求消息完全一致（原样重发，兼容旧行为）
        assert calls[1]["messages"] == calls[0]["messages"]

    def test_failed_attempt_traced(self, monkeypatch):
        calls, traced, outcome = _run_generate(
            monkeypatch, ['{"stem": "s", "answer": "Z"}', '{"stem": "s", "answer": "A"}']
        )
        assert len(traced) == 2
        failed, ok = traced
        assert failed["attempt"] == 1 and failed["success"] is False
        assert "error_summary" in failed["output_data"]
        assert ok["attempt"] == 2 and ok["success"] is True
        assert ok["output_data"] == {"stem": "s", "answer": "A"}


class TestExplicitOutputCap:
    def test_generation_passes_configured_max_tokens(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_MAX_OUTPUT_TOKENS", 8192)
        calls, _, outcome = _run_generate(monkeypatch, ['{"stem": "s", "answer": "A"}'])
        assert outcome["value"] == {"stem": "s", "answer": "A"}
        # 供应商默认输出上限（deepseek 4096）会截断长题 JSON，必须显式传递配置上限
        assert all(call["max_tokens"] == 8192 for call in calls)
