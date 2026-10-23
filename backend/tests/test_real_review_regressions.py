"""Frozen teaching and audit contracts through evaluation services and fake transports."""

import copy
import json

import pytest
import yaml
from test_real_generation import BACKEND, VALID, FakeClient
from test_real_judge import judge_manifest
from test_real_reference import manifest_fixture, review, solve

from app.engine.real_evaluation import run_generation
from app.engine.real_judge import run_judge
from app.engine.real_reference import run_reference_review


@pytest.mark.parametrize("unknown", [False, True])
def test_reference_snapshots_replay_actual_requests_through_format_repair_and_resume(
    tmp_path, unknown
):
    manifest = manifest_fixture()
    responses = [{}, TimeoutError()] if unknown else [{}, solve(), review()]
    client = FakeClient(responses)
    traces = []
    path = tmp_path / "run.json"
    run_reference_review(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len({trace["trace_id"] for trace in traces}) == len(client.requests)
    for index, (request, trace) in enumerate(zip(client.requests, traces)):
        snapshot = trace["snapshot_data"]
        assert snapshot["messages"] == request["messages"]
        assert snapshot["model"] == request["model"]
        assert snapshot["request_kwargs"] == {
            key: value for key, value in request.items() if key not in {"model", "messages"}
        }
        assert snapshot["settings"] == manifest["frozen"]["config"]["reference"]
        assert snapshot["model_profile"] == manifest["frozen"]["config"]["profiles"]["reference"]
        expected = None if unknown and index == 1 else json.dumps(responses[index])
        assert snapshot["response_text"] == expected
    assert len(traces[1]["snapshot_data"]["messages"]) == 4
    saved = json.loads(path.read_text("utf-8"))
    run_reference_review(path, saved, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(traces) == len(client.requests) == len(responses)


@pytest.mark.parametrize(
    "judge_labels,reference_labels",
    [([True] * 3, [True, False, False]), ([True, False, False], [True] * 3)],
)
def test_one_constant_model_margin_has_no_kappa_but_retains_pairs(
    tmp_path, judge_labels, reference_labels
):
    from app.engine.real_experiment import build_evidence_report

    manifest = manifest_fixture()
    manifest["judge_results"] = {}
    responses = []
    for index, (planned, judge_pass, reference_pass) in enumerate(
        zip(manifest["execution_plan"], judge_labels, reference_labels)
    ):
        path_id = planned["path_id"]
        manifest["generation_results"][path_id]["payload"]["stem"] = f"Distinct question {index}?"
        manifest["judge_results"][path_id] = {"state": "completed", "passed": judge_pass}
        evaluated = review()
        if not reference_pass:
            evaluated["questions"][0]["rubric"]["ambiguity"]["verdict"] = "fail"
        responses.extend([solve(), evaluated])
    run_reference_review(
        tmp_path / "run.json", manifest, client=FakeClient(responses), trace_sink=lambda **_: None
    )
    pairwise = manifest["reference_accounting"]["pairwise"]
    report = build_evidence_report(manifest)["model_agreement"]
    assert pairwise["denominator"] == report["paired"] == report["paired_paths"] == 3
    assert pairwise["kappa"] is report["kappa"] is None
    assert pairwise["kappa_status"] == report["kappa_status"] == "unavailable_degenerate_marginals"
    assert pairwise["agreement_rate"] == report["agreement"] == 1 / 3


@pytest.mark.parametrize("template_id,count", [("single_choice", 1), ("cloze", 5), ("reading", 3)])
def test_all_frozen_teaching_requirements_reach_every_model_role(tmp_path, template_id, count):
    manifest = manifest_fixture()
    judge_config = judge_manifest()["frozen"]["config"]
    config = manifest["frozen"]["config"]
    config["judge"] = judge_config["judge"]
    config["judge"]["calibration"] = {}
    config["profiles"]["judge"] = judge_config["profiles"]["judge"]
    config["prices"]["fixture-judge"] = judge_config["prices"]["fixture-judge"]
    case = manifest["frozen"]["dataset"]["sets"]["exploration"][0]
    case.update(
        template_id=template_id,
        teaching_goal="goal-marker",
        scope="scope-marker",
        judgment_basis="basis-marker",
        answer="hidden-case-answer",
        verdict="hidden-verdict",
    )
    case["params"].update(topic="topic-marker", answer="hidden-param-answer")
    if template_id != "single_choice":
        case["params"]["blank_count" if template_id == "cloze" else "question_count"] = count
    template = yaml.safe_load((BACKEND / f"app/templates/{template_id}.yaml").read_text("utf-8"))
    manifest["frozen"]["templates"][template_id] = template
    manifest["execution_plan"] = manifest["execution_plan"][:1]
    manifest["execution_plan"][0]["state"] = "planned"
    manifest["generation_results"] = {}
    payload = copy.deepcopy(VALID)
    if template_id != "single_choice":
        payload = {
            "passage": (
                " ".join(["____"] * count) if template_id == "cloze" else "A test-only passage."
            ),
            "blanks" if template_id == "cloze" else "questions": [
                {**copy.deepcopy(VALID), "index": i} for i in range(1, count + 1)
            ],
        }
    generation = FakeClient([payload])
    judge = FakeClient(
        [{"dimension_scores": {rule["id"]: 90 for rule in template["quality_rules"]}}]
    )
    reference = FakeClient([solve(count), review(count)])
    traces = []
    path = tmp_path / "run.json"
    run_generation(path, manifest, client=generation, trace_sink=lambda **row: traces.append(row))
    run_judge(path, manifest, client=judge, trace_sink=lambda **row: traces.append(row))
    run_reference_review(
        path, manifest, client=reference, trace_sink=lambda **row: traces.append(row)
    )
    assert len(traces) == 4
    for request in [*generation.requests, *judge.requests, *reference.requests]:
        messages = json.dumps(request["messages"], ensure_ascii=False)
        for marker in (
            "goal-marker",
            "scope-marker",
            "basis-marker",
            "topic-marker",
            "self-authored revision",
        ):
            assert marker in messages
        assert "hidden-case-answer" not in messages
        assert "hidden-param-answer" not in messages
        assert "hidden-verdict" not in messages
    blind_packet = json.loads(reference.requests[0]["messages"][1]["content"].split("\n", 1)[1])
    assert "answer" not in json.dumps(blind_packet)
    assert "explanation" not in json.dumps(blind_packet)
    assert "feedback_retry" not in json.dumps(blind_packet)
    for trace, request in zip(traces, [*generation.requests, *judge.requests, *reference.requests]):
        snapshot = trace["snapshot_data"]
        assert "goal-marker" in json.dumps(snapshot["messages"])
        assert snapshot["messages"] == request["messages"]
        assert snapshot["temperature"] == request["temperature"]
        assert snapshot["max_tokens"] == request["max_tokens"]
        assert snapshot["model"] == request["model"]
