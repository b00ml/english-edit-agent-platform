import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.engine.quality import _validate_dimension_scores, run_quality_check
from app.errors import QualityCheckError


@pytest.mark.parametrize(
    "scores",
    [
        {},
        {"a": 80},
        {"a": -1, "b": 80},
        {"a": 101, "b": 80},
        {"a": True, "b": 80},
        {"a": "80", "b": 80},
        {"a": float("nan"), "b": 80},
        {"a": float("inf"), "b": 80},
        {"a": 80, "b": 80, "other": 100},
        [],
    ],
)
def test_dimension_scores_fail_closed(scores):
    with pytest.raises(ValidationError):
        _validate_dimension_scores(scores, ["a", "b"])


def setup_judge(monkeypatch, responses):
    template = SimpleNamespace(
        quality_rules=[{"id": "a", "weight": 0.5}, {"id": "b", "weight": 0.5}],
        run_config={"judge_max_retry": 2},
        version=1,
    )
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    call = Mock(side_effect=[(json.dumps({"dimension_scores": d}), 1.0, usage) for d in responses])
    trace = Mock()
    monkeypatch.setattr("app.engine.quality._get_openai_client", lambda: None)
    monkeypatch.setattr("app.engine.quality._call_judge_once", call)
    monkeypatch.setattr("app.engine.quality.record_trace", trace)
    return template, call, trace


def test_invalid_round_retried_with_failed_trace(monkeypatch):
    template, call, trace = setup_judge(monkeypatch, [{"a": 100}, {"a": 80, "b": 90}])
    score, dims = run_quality_check(template, {}, trace_id="t:0", rounds=1)
    assert score == 85 and dims == {"a": 80, "b": 90}
    assert call.call_count == 2
    assert [c.kwargs["success"] for c in trace.call_args_list] == [False, True]
    assert all(c.kwargs["cost"] > 0 for c in trace.call_args_list)


def test_invalid_round_exhaustion_is_not_partial_success(monkeypatch):
    template, call, trace = setup_judge(monkeypatch, [{"a": 80, "b": 90}, {}, {}])
    with pytest.raises(QualityCheckError, match="第 2 轮耗尽 2 次"):
        run_quality_check(template, {}, trace_id="t:0", rounds=2)
    assert call.call_count == 3
    assert [c.kwargs["success"] for c in trace.call_args_list] == [True, False, False]


def test_rule_ids_use_aliases_not_pydantic_reserved_names():
    assert _validate_dimension_scores({"_rule": 80, "model_dump": 90}, ["_rule", "model_dump"]) == {
        "_rule": 80.0,
        "model_dump": 90.0,
    }
