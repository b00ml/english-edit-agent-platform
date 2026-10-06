import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.engine.structured_output import generate_structured
from app.errors import StructuredOutputError


def setup_generation(monkeypatch, responses):
    template = SimpleNamespace(
        type_id="single_choice",
        version=1,
        run_config={"max_retry": 2},
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
        output_schema={
            "type": "object",
            "required": ["options"],
            "properties": {
                "options": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
            },
        },
    )
    messages = []

    def call(client, model, current_messages):
        messages.append(copy.deepcopy(current_messages))
        return (
            json.dumps(responses.pop(0)),
            5.0,
            SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    trace = Mock()
    monkeypatch.setattr("app.engine.structured_output._get_openai_client", lambda: None)
    monkeypatch.setattr("app.engine.structured_output._call_openai_json", call)
    monkeypatch.setattr("app.engine.structured_output.record_trace", trace)
    return template, messages, trace


def test_json_schema_retry_injects_field_feedback_and_cost(monkeypatch):
    template, messages, trace = setup_generation(
        monkeypatch,
        [
            {"options": ["A", "A"]},
            {"options": ["A", "B"]},
        ],
    )
    result = generate_structured(template, {}, None, trace_id="t:0")
    assert result == {"options": ["A", "B"]}
    assert len(messages) == 2
    assert "JSON Schema uniqueItems" in messages[1][-1]["content"]
    assert "options" in messages[1][-1]["content"]
    assert [c.kwargs["success"] for c in trace.call_args_list] == [False, True]
    assert all(c.kwargs["cost"] > 0 for c in trace.call_args_list)


def test_json_schema_retry_exhaustion_has_explicit_error(monkeypatch):
    template, messages, trace = setup_generation(
        monkeypatch,
        [
            {"options": ["A", "A"]},
            {"options": ["A", "A"]},
        ],
    )
    with pytest.raises(StructuredOutputError, match="uniqueItems"):
        generate_structured(template, {}, None, trace_id="t:0")
    assert len(messages) == 2
    assert [c.kwargs["attempt"] for c in trace.call_args_list] == [1, 2]
    assert all(not c.kwargs["success"] for c in trace.call_args_list)
