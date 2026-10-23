"""Blind executable reference review at the provider/Trace boundary, offline only."""

import copy
import json

import pytest
from test_real_generation import VALID, FakeClient, local_manifest

from app.engine.real_reference import run_reference_review


def manifest_fixture():
    manifest = local_manifest()
    config = manifest["frozen"]["config"]
    config["reference"] = {
        "temperature": 0.2,
        "timeout_seconds": 90,
        "max_output_tokens": 3072,
        "context_limit_tokens": 16384,
    }
    config["profiles"]["reference"] = {"model_name": "fixture-reference", "model_family": "ref"}
    config["prices"]["fixture-reference"] = config["prices"]["fixture-model"]
    manifest["protocol"]["reference_max_format_repairs"] = 2
    manifest["generation_results"] = {}
    for planned in manifest["execution_plan"]:
        planned["state"] = "completed"
        manifest["generation_results"][planned["path_id"]] = {"payload": copy.deepcopy(VALID)}
    return manifest


def solve(count=1):
    return {
        "answers": [
            {"index": i, "answer": "A", "reason": "Independent evidence."}
            for i in range(1, count + 1)
        ]
    }


def review(count=1):
    rubric = {
        name: {"verdict": "pass", "evidence": "Visible supporting evidence."}
        for name in (
            "answer_correctness",
            "explanation_correctness",
            "ambiguity",
            "knowledge_point_fit",
            "difficulty_fit",
        )
    }
    return {
        "questions": [{"index": i, "rubric": copy.deepcopy(rubric)} for i in range(1, count + 1)]
    }


@pytest.mark.parametrize("failure", ["price", "physical_cap"])
def test_local_reference_configuration_failure_keeps_no_phantom_source(tmp_path, failure):
    manifest = manifest_fixture()
    if failure == "price":
        del manifest["frozen"]["config"]["prices"]["fixture-reference"]
        error, message = KeyError, "fixture-reference"
    else:
        manifest["protocol"]["call_limits"]["reference_solve"]["physical"] = 0
        error, message = ValueError, "physical call limit"
    client = FakeClient([])
    traces = []
    path = tmp_path / "run.json"
    with pytest.raises(error, match=message):
        run_reference_review(
            path, manifest, client=client, trace_sink=lambda **row: traces.append(row)
        )
    saved = json.loads(path.read_text("utf-8"))
    assert client.requests == traces == saved["provider_calls"] == []
    result = saved["reference_results"]["single_attempt"]
    assert result["source_call_ids"] == []
    assert result["state"] == "failed" and result["error_type"] == error.__name__


def test_blind_fresh_phases_dedup_trace_and_independent_completion(tmp_path):
    manifest = manifest_fixture()
    manifest["judge_results"] = {
        "single_attempt": {"state": "completed", "passed": False, "score": 12},
        "blind_retry": {"state": "unresolved"},
    }
    client = FakeClient([solve(), review()])
    traces = []
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    assert len(client.requests) == len(traces) == len(manifest["provider_calls"]) == 2
    assert client.options == {"max_retries": 0, "timeout": 90}
    assert all(request["temperature"] == 0.2 for request in client.requests)
    assert all(request["max_tokens"] == 3072 for request in client.requests)
    first, second = [request["messages"] for request in client.requests]
    assert [m["role"] for m in first] == ["system", "user"]
    assert [m["role"] for m in second] == ["system", "user"]
    assert VALID["explanation"] not in json.dumps(first)
    assert VALID["explanation"] in json.dumps(second)
    assert "self-authored revision" in json.dumps(second)
    for secret in ("single_attempt", "blind_retry", "feedback_retry", "judge_results", "score"):
        assert secret not in json.dumps(first + second)
    rows = manifest["reference_results"]
    assert all(row["state"] == "completed" and row["reference_pass"] for row in rows.values())
    assert rows["single_attempt"]["judge_pass"] is False
    assert rows["blind_retry"]["judge_pass"] is None
    assert manifest["reference_accounting"]["pairwise"]["denominator"] == 1
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    assert len(client.requests) == len(traces) == 2


@pytest.mark.parametrize("template_id,count", [("single_choice", 1), ("cloze", 5), ("reading", 3)])
def test_allowlist_counts_and_any_subquestion_failure_fails_document(tmp_path, template_id, count):
    manifest = manifest_fixture()
    case = manifest["frozen"]["dataset"]["sets"]["exploration"][0]
    case["template_id"] = template_id
    payload = copy.deepcopy(VALID)
    payload["meta"] = {"answer": "Hidden answer label"}
    if template_id != "single_choice":
        payload = {
            "passage": "Test-only passage.",
            "blanks" if template_id == "cloze" else "questions": [
                {**copy.deepcopy(VALID), "index": i} for i in range(1, count + 1)
            ],
            "explanation": "Hidden passage explanation",
        }
    for row in manifest["generation_results"].values():
        row["payload"] = payload
    failed = review(count)
    failed["questions"][-1]["rubric"]["explanation_correctness"]["verdict"] = "fail"
    client = FakeClient([solve(count), failed])
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **_: None
    )
    packet = json.loads(client.requests[0]["messages"][1]["content"].split("\n", 1)[1])
    assert len(packet["questions"]) == count
    assert "explanation" not in json.dumps(packet)
    assert "answer" not in json.dumps(packet)
    assert "meta" not in json.dumps(packet)
    assert all(row["reference_pass"] is False for row in manifest["reference_results"].values())


@pytest.mark.parametrize("phase", ["solve", "review"])
def test_format_repairs_capped_per_phase_and_failed_calls_traced(tmp_path, phase):
    manifest = manifest_fixture()
    responses = [{}, {}, {}] if phase == "solve" else [solve(), {}, {}, {}]
    client = FakeClient(responses)
    traces = []
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    assert len(client.requests) == (3 if phase == "solve" else 4)
    assert sum(row["success"] is False for row in traces) == 3
    assert all(row["state"] == "failed" for row in manifest["reference_results"].values())
    assert manifest["reference_accounting"]["pairwise"]["denominator"] == 0


@pytest.mark.parametrize("phase", ["solve", "review"])
def test_unknown_acceptance_preserved_without_format_or_resume_replay(tmp_path, phase):
    manifest = manifest_fixture()
    responses = [TimeoutError()] if phase == "solve" else [solve(), TimeoutError()]
    client = FakeClient(responses)
    path = tmp_path / "run.json"
    run_reference_review(path, manifest, client=client, trace_sink=lambda **_: None)
    assert len(client.requests) == len(responses)
    assert all(row["state"] == "unresolved" for row in manifest["reference_results"].values())
    run_reference_review(path, manifest, client=client, trace_sink=lambda **_: None)
    assert len(client.requests) == len(responses)
    assert manifest["reference_accounting"]["physical_cost"] is None


@pytest.mark.parametrize("invalid", ["indices", "dimension", "evidence", "contradiction", "total"])
def test_review_validation_repairs_and_fresh_phase_history(tmp_path, invalid):
    manifest = manifest_fixture()
    broken = review()
    if invalid == "indices":
        broken["questions"][0]["index"] = 2
    elif invalid == "dimension":
        del broken["questions"][0]["rubric"]["ambiguity"]
    elif invalid == "evidence":
        broken["questions"][0]["rubric"]["ambiguity"]["evidence"] = "  "
    elif invalid == "contradiction":
        for row in manifest["generation_results"].values():
            row["payload"]["answer"] = "B"
    else:
        broken["reference_pass"] = True
    fixed = review()
    if invalid == "contradiction":
        fixed["questions"][0]["rubric"]["answer_correctness"]["verdict"] = "fail"
    client = FakeClient([solve(), broken, fixed])
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **_: None
    )
    assert len(client.requests) == 3
    assert [len(r["messages"]) for r in client.requests] == [2, 2, 4]
    assert all(row["state"] == "completed" for row in manifest["reference_results"].values())


def test_reference_uncertainty_excluded_and_missing_paths_visible(tmp_path):
    manifest = manifest_fixture()
    manifest["execution_plan"][1]["state"] = "failed"
    manifest["execution_plan"][2]["state"] = "cancelled"
    uncertain = review()
    uncertain["questions"][0]["rubric"]["difficulty_fit"]["verdict"] = "unresolved"
    client = FakeClient([solve(), uncertain])
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **_: None
    )
    assert [r["state"] for r in manifest["reference_results"].values()] == [
        "unresolved",
        "missing",
        "cancelled",
    ]
    assert manifest["reference_accounting"]["full_plan_denominator"] == 3
    assert manifest["reference_accounting"]["pairwise"]["denominator"] == 0


def test_judge_link_can_be_added_after_independent_review_without_provider_replay(tmp_path):
    manifest = manifest_fixture()
    client = FakeClient([solve(), review()])
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **_: None
    )
    manifest["judge_results"] = {"single_attempt": {"state": "completed", "passed": True}}
    run_reference_review(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **_: None
    )
    assert len(client.requests) == 2
    assert manifest["reference_accounting"]["pairwise"]["denominator"] == 1
    assert manifest["reference_accounting"]["pairwise"]["kappa"] is None
