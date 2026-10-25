"""Frozen production Judge behavior through the public service and local ledger."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_real_generation import VALID, FakeClient, local_manifest

from app.engine.quality import run_quality_check
from app.engine.real_evaluation import evaluation_template
from app.engine.real_judge import run_judge
from app.errors import QualityCheckError


def judge_manifest() -> dict[str, Any]:
    manifest = local_manifest()
    config = manifest["frozen"]["config"]
    config["profiles"]["judge"] = {"model_name": "fixture-judge", "profile_hash": "frozen"}
    config["prices"]["fixture-judge"] = copy.deepcopy(config["prices"]["fixture-model"])
    config["judge"] = {
        "temperature": 0.1,
        "timeout_seconds": 30,
        "max_output_tokens": 1024,
        "context_limit_tokens": 8192,
        "sample_rounds": 1,
        "max_attempts": 2,
        "calibration": {"single_choice": {"threshold": 80.0}},
    }
    manifest["generation_results"] = {}
    for planned in manifest["execution_plan"]:
        planned["state"] = "completed"
        manifest["generation_results"][planned["path_id"]] = {"payload": copy.deepcopy(VALID)}
    return manifest


@pytest.mark.parametrize(
    "error", [AttributeError("local adapter"), QualityCheckError("local adapter")]
)
def test_judge_adapter_failure_does_not_become_output_validation_or_retry(error):
    manifest = judge_manifest()
    case = manifest["frozen"]["dataset"]["sets"]["exploration"][0]
    template = evaluation_template(manifest, case, 1)
    requests = []
    traces = []

    def adapter(messages, sample_round, attempt):
        requests.append((sample_round, attempt))
        raise error

    with pytest.raises(type(error)) as caught:
        run_quality_check(
            template,
            VALID,
            model_name="fixture-judge",
            rounds=1,
            response_adapter=adapter,
            trace_sink=lambda **row: traces.append(row),
            max_attempts=2,
        )
    assert caught.value is error
    assert requests == [(1, 1)]
    assert traces == []


@pytest.mark.parametrize("failure", ["price", "physical_cap"])
def test_local_judge_configuration_failure_keeps_no_phantom_source(tmp_path, failure):
    manifest = judge_manifest()
    if failure == "price":
        del manifest["frozen"]["config"]["prices"]["fixture-judge"]
        error, message = KeyError, "fixture-judge"
    else:
        manifest["protocol"]["call_limits"]["qc"]["physical"] = 0
        error, message = ValueError, "physical call limit"
    client = FakeClient([])
    traces = []
    path = tmp_path / "run.json"
    with pytest.raises(error, match=message):
        run_judge(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    saved = json.loads(path.read_text("utf-8"))
    assert client.requests == traces == saved["provider_calls"] == []
    result = saved["judge_results"]["single_attempt"]
    assert result["source_call_ids"] == []
    assert result["state"] == "failed" and result["error_type"] == error.__name__


def test_shared_content_uses_production_weighted_judge_and_one_physical_trace(
    tmp_path: Path,
) -> None:
    manifest = judge_manifest()
    client = FakeClient(
        [
            {
                "dimension_scores": {
                    "kp_match": 80,
                    "diff_match": 90,
                    "distractor": 70,
                    "unambiguous": 100,
                }
            }
        ]
    )
    traces = []
    path = tmp_path / "run.json"
    run_judge(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    saved = json.loads(path.read_text("utf-8"))
    assert len(client.requests) == len(traces) == len(saved["provider_calls"]) == 1
    assert client.requests[0]["temperature"] == 0.1
    assert client.requests[0]["max_tokens"] == 1024
    assert "self-authored revision" in client.requests[0]["messages"][1]["content"]
    assert client.options == {"max_retries": 0, "timeout": 30}
    assert all(
        row["score"] == 83.0 and row["passed"] is True and row["threshold"] == 80.0
        for row in saved["judge_results"].values()
    )
    assert len({row["content_id"] for row in saved["judge_results"].values()}) == 1
    assert traces[0]["stage"] == "qc" and traces[0]["cost"] == 0.2
    assert (
        traces[0]["input_data"]["model_profile"]
        == manifest["frozen"]["config"]["profiles"]["judge"]
    )
    assert traces[0]["input_data"]["threshold"] == 80.0
    assert saved["judge_accounting"]["physical_cost"] == 0.2
    assert saved["judge_accounting"]["pass_rate_all_paths"]["denominator"] == 3
    run_judge(path, saved, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1


def test_repeated_logical_paths_keep_estimated_cost_without_rebilling(tmp_path: Path) -> None:
    manifest = judge_manifest()
    manifest["execution_plan"][1]["arm"] = "single_attempt"
    client = FakeClient(
        [
            {
                "dimension_scores": {
                    rule["id"]: 90
                    for rule in manifest["frozen"]["templates"]["single_choice"]["quality_rules"]
                }
            }
        ]
    )
    run_judge(tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: None)
    assert manifest["judge_accounting"]["physical_cost"] == 0.2
    assert manifest["judge_accounting"]["arms"]["single_attempt"]["logical_cost"] == 0.4
    assert len(client.requests) == 1


def test_missing_generation_and_failed_judge_have_independent_denominators(tmp_path: Path) -> None:
    manifest = judge_manifest()
    manifest["execution_plan"][0]["state"] = "failed"
    manifest["generation_results"]["single_attempt"] = {"error": "generation failed"}
    manifest["execution_plan"][1]["state"] = "cancelled"
    client = FakeClient([{}, {}])
    traces = []
    run_judge(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    assert [row["state"] for row in manifest["execution_plan"]] == [
        "failed",
        "cancelled",
        "completed",
    ]
    assert [
        manifest["judge_results"][arm]["state"]
        for arm in ("single_attempt", "blind_retry", "feedback_retry")
    ] == ["missing", "cancelled", "failed"]
    accounting = manifest["judge_accounting"]
    assert accounting["state_counts"] == {"missing": 1, "cancelled": 1, "failed": 1}
    assert accounting["pass_rate_all_paths"] == {"numerator": 0, "denominator": 3, "rate": 0.0}
    assert accounting["pass_rate_schema_valid"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert accounting["pass_rate_completed"]["rate"] is None
    assert len(client.requests) == len(traces) == 2
    assert all(row["success"] is False for row in traces)


def test_frozen_rounds_retries_weights_and_zero_threshold_override_live_defaults(
    tmp_path: Path,
) -> None:
    manifest = judge_manifest()
    manifest["frozen"]["config"]["judge"].update(
        sample_rounds=2,
        max_attempts=2,
        calibration={
            "single_choice": {
                "threshold": 0.0,
                "kp_match": 1.0,
                "diff_match": 0.0,
                "distractor": 0.0,
                "unambiguous": 0.0,
            }
        },
    )

    def scores(score: float) -> dict[str, dict[str, float]]:
        return {
            "dimension_scores": {
                "kp_match": score,
                "diff_match": 100,
                "distractor": 100,
                "unambiguous": 100,
            }
        }

    client = FakeClient([{}, scores(0), scores(20)])
    traces = []
    run_judge(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    result = manifest["judge_results"]["single_attempt"]
    assert result["score"] == 10.0 and result["dimension_scores"]["kp_match"] == 10.0
    assert result["threshold"] == 0.0 and result["passed"] is True
    assert result["weights"]["diff_match"] == 0.0
    assert [row["success"] for row in traces] == [False, True, True]
    assert [row["input_data"]["sample_round"] for row in traces] == [1, 1, 2]
    assert len(client.requests) == 3


def test_unknown_judge_acceptance_is_unresolved_without_touching_reference_or_resending(
    tmp_path: Path,
) -> None:
    manifest = judge_manifest()
    manifest["reference_results"] = {"single_attempt": {"state": "completed", "passed": False}}
    client = FakeClient([TimeoutError("unknown acceptance")])
    traces = []
    path = tmp_path / "run.json"
    run_judge(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1
    assert all(row["state"] == "unresolved" for row in manifest["judge_results"].values())
    assert manifest["judge_accounting"]["physical_cost"] is None
    assert manifest["judge_accounting"]["pass_rate_schema_valid"]["denominator"] == 3
    assert manifest["reference_results"]["single_attempt"] == {
        "state": "completed",
        "passed": False,
    }
    run_judge(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1


def test_crash_after_response_reuses_judge_and_exposes_unresolved_trace(tmp_path: Path) -> None:
    manifest = judge_manifest()
    client = FakeClient(
        [
            {
                "dimension_scores": {
                    "kp_match": 100,
                    "diff_match": 100,
                    "distractor": 100,
                    "unambiguous": 100,
                }
            }
        ]
    )
    path = tmp_path / "run.json"

    def crash(**row: Any) -> None:
        raise RuntimeError("simulated Trace interruption")

    with pytest.raises(RuntimeError, match="Trace interruption"):
        run_judge(path, manifest, client=client, trace_sink=crash)
    saved = json.loads(path.read_text("utf-8"))
    run_judge(path, saved, client=client, trace_sink=lambda **row: pytest.fail("Trace resent"))
    assert len(client.requests) == 1
    assert all(row["state"] == "completed" for row in saved["judge_results"].values())
    assert saved["judge_accounting"]["trace_coverage"]["complete"] is False
    assert len(saved["judge_accounting"]["trace_coverage"]["unresolved_call_ids"]) == 1
