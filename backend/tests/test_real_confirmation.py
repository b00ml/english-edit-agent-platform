"""Confirmation contracts exercise real services with temporary data only."""

import copy
import json
from pathlib import Path

import pytest

from app.engine.evaluation_protocol import canonical_hash, freeze_protocol

pytest_plugins = ["test_real_quality_protocol"]
BACKEND = Path(__file__).parents[1]


@pytest.fixture
def exploration(tmp_path, dataset_path, runtime_config):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    return freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="fixture-exploration",
        seed=42,
    )


def test_no_change_is_immutable_and_schedules_no_calls(exploration):
    from app.engine.real_confirmation import confirmation_report, freeze_confirmation

    bundle = freeze_confirmation(
        exploration,
        {"decision": "no_change", "reason": "No reproducible support"},
        run_id="fixture-confirm",
        seed=13,
    )
    report = confirmation_report(bundle)
    assert bundle["execution_plan"] == []
    assert report["planned_main_paths"] == 0
    assert report["physical"]["call_count"] == 0
    bundle["selection"]["reason"] = "changed after observing outcomes"
    with pytest.raises(ValueError, match="immutable"):
        confirmation_report(bundle)


def choice(
    source,
    factor="feedback_prompt",
    key="retry-feedback.st",
    new="FIXTURE FEEDBACK {{validation_error}}",
):
    path = source["execution_plan"][0]
    path["state"] = "failed"
    source["generation_results"] = {path["path_id"]: {"error": "reproducible fixture schema error"}}
    return {
        "decision": "change",
        "factor": factor,
        "key": key,
        "new": new,
        "hypothesis": "Fixture evidence suggests this single factor helps",
        "analysis": "Paired by input; repeated rounds; planned denominators; signed differences",
        "evidence_refs": [path["path_id"]],
    }


def test_feedback_plan_is_paired_interleaved_and_has_separate_blind_control(exploration):
    from app.engine.real_confirmation import confirmation_report, freeze_confirmation

    bundle = freeze_confirmation(exploration, choice(exploration), run_id="confirm", seed=13)
    plan = bundle["execution_plan"]
    assert len(plan) == 540
    assert sum(row["version"] != "blind_control" for row in plan) == 360
    assert len({row["case_id"] for row in plan}) == 60
    for offset in range(0, len(plan), 3):
        block = plan[offset : offset + 3]
        assert {row["version"] for row in block} == {"old", "new", "blind_control"}
        assert len({row["first_draft_id"] for row in block}) == 1
        assert len({(row["case_id"], row["round"]) for row in block}) == 1
    assert confirmation_report(bundle)["planned_main_paths"] == 360
    assert (
        freeze_confirmation(exploration, choice(exploration), run_id="confirm", seed=13)["identity"]
        == bundle["identity"]
    )
    invalid = choice(exploration)
    invalid["evidence_refs"] = ["confirmation-outcome"]
    with pytest.raises(ValueError, match="exploration evidence"):
        freeze_confirmation(exploration, invalid, run_id="invalid", seed=1)
    invalid = choice(exploration)
    invalid["second_factor"] = "threshold"
    with pytest.raises(ValueError, match="single factor"):
        freeze_confirmation(exploration, invalid, run_id="invalid", seed=1)


def test_changed_generation_prompt_is_executed_from_snapshot_with_independent_drafts(
    tmp_path, exploration
):
    from test_real_generation import VALID, FakeClient

    from app.engine.real_confirmation import freeze_confirmation
    from app.engine.real_evaluation import run_generation

    selection = choice(
        exploration,
        "generation_prompt",
        "single-choice-system.st",
        "FROZEN NEW SYSTEM {{difficulty}}",
    )
    selection["key"] = exploration["frozen"]["templates"]["single_choice"]["gen_prompt"][
        "system"
    ].removeprefix("prompts/")
    bundle = freeze_confirmation(exploration, selection, run_id="prompt-confirm", seed=1)
    rows = [
        row
        for row in bundle["execution_plan"]
        if row["case_id"] == "confirmation-single_choice-0" and row["round"] == 1
    ]
    assert len({row["first_draft_id"] for row in rows}) == 2
    client = FakeClient([VALID, VALID])
    for row in rows:
        run_generation(
            tmp_path / "unused.json",
            bundle["runs"][row["run_key"]],
            client=client,
            path_ids={row["path_id"]},
            persist=lambda: None,
            trace_sink=lambda **_: None,
        )
    systems = [request["messages"][0]["content"] for request in client.requests]
    assert sum("FROZEN NEW SYSTEM" in text for text in systems) == 1
    assert all(
        bundle["runs"][row["run_key"]]["generation_results"][row["path_id"]]["payload"] == VALID
        for row in rows
    )
    cancelled = next(
        row for row in bundle["runs"]["old:1"]["execution_plan"] if row["state"] == "planned"
    )
    cancelled["state"] = "cancelled"
    no_send = FakeClient([])
    run_generation(
        tmp_path / "unused.json",
        bundle["runs"]["old:1"],
        client=no_send,
        path_ids={cancelled["path_id"]},
        persist=lambda: None,
        trace_sink=lambda **_: None,
    )
    assert cancelled["state"] == "cancelled"
    assert no_send.requests == []


@pytest.mark.parametrize("factor", ["judge_threshold", "judge_weights"])
def test_policy_reuses_raw_scores_zero_calls_and_keeps_missing_repeated_observations(
    tmp_path, exploration, factor
):
    from test_real_generation import VALID

    from app.engine.real_confirmation import (
        confirmation_report,
        freeze_confirmation,
        run_confirmation,
    )

    rules = exploration["frozen"]["templates"]["single_choice"]["quality_rules"]
    weights = {rule["id"]: float(index == 0) for index, rule in enumerate(rules)}
    selection = choice(
        exploration, factor, "single_choice", 71.0 if factor == "judge_threshold" else weights
    )
    row = next(
        row
        for row in exploration["execution_plan"]
        if row["arm"] == "feedback_retry" and row["case_id"] == "exploration-single_choice-0"
    )
    row["state"] = "completed"
    exploration["generation_results"][row["path_id"]] = {"payload": VALID}
    dimensions = {rule["id"]: 70.004 for rule in rules}
    if factor == "judge_weights":
        dimensions = {rule["id"]: float(100 * (index != 0)) for index, rule in enumerate(rules)}
    exploration["judge_results"] = {
        row["path_id"]: {
            "state": "completed",
            "dimension_scores": dimensions,
            "passed": True,
            "score": 70.0,
        }
    }
    exploration["reference_results"] = {
        row["path_id"]: {"state": "completed", "reference_pass": True}
    }
    observations = []
    if factor == "judge_weights":
        observation = copy.deepcopy(exploration)
        observation["run_id"] = "recorded-round-two"
        recorded = next(
            item for item in observation["execution_plan"] if item["path_id"] == row["path_id"]
        )
        recorded["round"] = 2
        observation["identities"]["execution_plan"] = canonical_hash(
            {
                "protocol": observation["protocol"],
                "paths": [{**item, "state": "planned"} for item in observation["execution_plan"]],
            }
        )
        observations.append(observation)
    bundle = freeze_confirmation(
        exploration, selection, run_id="threshold-confirm", seed=2, observations=observations
    )
    run_confirmation(tmp_path / "confirm.json", bundle, clients={})
    report = confirmation_report(bundle)
    assert report["physical"]["call_count"] == 0
    assert report["planned_main_paths"] == 360
    assert report["paired_inputs"] == 1
    assert report["contrasts"]["judge"]["paired_observations"] == 1 + len(observations)
    assert report["contrasts"]["judge"]["change_percentage_points"] == -100.0
    assert report["unique_inputs"] == 60
    assert report["groups"]["new:exploration"]["stages"]["judge"]["missing"] == 89 - len(
        observations
    )
    # Production rounds total to two decimals before the policy comparison.
    target = next(
        row
        for row in bundle["execution_plan"]
        if row["case_id"] == "exploration-single_choice-0"
        and row["round"] == 1
        and row["version"] == "new"
    )
    assert bundle["runs"][target["run_key"]]["judge_results"][target["path_id"]]["score"] == (
        70.0 if factor == "judge_threshold" else 0.0
    )
    if observations:
        second = next(
            item
            for item in bundle["execution_plan"]
            if item["case_id"] == row["case_id"] and item["round"] == 2 and item["version"] == "new"
        )
        inherited = bundle["runs"][second["run_key"]]["judge_results"][second["path_id"]]
        assert inherited["inherited_from"]["run_id"] == "recorded-round-two"
        assert inherited["inherited_from"]["source_hash"] == canonical_hash(observations[0])
    saved = json.loads((tmp_path / "confirm.json").read_text("utf-8"))
    saved["runs"][target["run_key"]]["frozen"]["prompts"]["judge-system.st"] = "tampered"
    with pytest.raises(ValueError, match="immutable"):
        confirmation_report(saved)


def test_judge_variants_do_not_reuse_each_others_judgment_and_reference_reads_frozen_assets(
    tmp_path, exploration
):
    from test_real_generation import VALID, FakeClient
    from test_real_reference import review, solve

    from app.engine.real_confirmation import freeze_confirmation
    from app.engine.real_judge import run_judge
    from app.engine.real_reference import run_reference_review

    selection = choice(exploration, "judge_prompt", "judge-system.st", "FROZEN NEW JUDGE")
    # The immutable source, including its reference assets, is frozen before selection.
    exploration["frozen"]["prompts"]["reference-solve-system.st"] = "FROZEN SOLVER {{skill}}"
    exploration["frozen"]["skills"]["skills/reference_review/SKILL.md"] = "FROZEN REFERENCE SKILL"
    for name in ("prompts", "skills"):
        exploration["identities"][name] = canonical_hash(exploration["frozen"][name])
    bundle = freeze_confirmation(exploration, selection, run_id="judge-confirm", seed=3)
    rows = [
        row
        for row in bundle["execution_plan"]
        if row["case_id"] == "confirmation-single_choice-0" and row["round"] == 1
    ]
    scores = {
        rule["id"]: 90
        for rule in exploration["frozen"]["templates"]["single_choice"]["quality_rules"]
    }
    client = FakeClient([{"dimension_scores": scores}, {"dimension_scores": scores}])
    reference = FakeClient([solve(), review()])
    for row in rows:
        run = bundle["runs"][row["run_key"]]
        path = next(path for path in run["execution_plan"] if path["path_id"] == row["path_id"])
        path["state"] = "completed"
        run["generation_results"] = {row["path_id"]: {"payload": VALID}}
        run_judge(
            tmp_path / "unused.json",
            run,
            client=client,
            path_ids={row["path_id"]},
            persist=lambda: None,
            trace_sink=lambda **_: None,
        )
    assert len(client.requests) == 2
    assert (
        sum("FROZEN NEW JUDGE" in request["messages"][0]["content"] for request in client.requests)
        == 1
    )
    row = rows[0]
    run_reference_review(
        tmp_path / "unused.json",
        bundle["runs"][row["run_key"]],
        client=reference,
        path_ids={row["path_id"]},
        persist=lambda: None,
        trace_sink=lambda **_: None,
    )
    assert "FROZEN SOLVER" in reference.requests[0]["messages"][0]["content"]
    assert "FROZEN REFERENCE SKILL" in reference.requests[0]["messages"][0]["content"]


def test_selection_rejects_source_identity_drift(exploration):
    from app.engine.real_confirmation import freeze_confirmation

    selection = choice(exploration)
    exploration["frozen"]["prompts"]["judge-system.st"] = "unfrozen change"
    with pytest.raises(ValueError, match="source identity drift"):
        freeze_confirmation(exploration, selection, run_id="invalid-source", seed=1)


def test_signed_effects_weight_inputs_equally_with_unequal_repeated_coverage(exploration):
    from app.engine.real_confirmation import confirmation_report, freeze_confirmation

    bundle = freeze_confirmation(exploration, choice(exploration), run_id="effects", seed=1)
    for row in bundle["execution_plan"]:
        case, repeat, version = row["case_id"], row["round"], row["version"]
        if (
            version == "blind_control"
            or case not in {"confirmation-single_choice-0", "confirmation-single_choice-1"}
            or (case.endswith("-1") and repeat != 1)
        ):
            continue
        passed = (version == "old") if case.endswith("-0") else (version == "new")
        run = bundle["runs"][row["run_key"]]
        run.setdefault("judge_results", {})[row["path_id"]] = {
            "state": "completed",
            "passed": passed,
        }
    report = confirmation_report(bundle)
    assert report["contrasts"]["judge"]["paired_observations"] == 4
    assert report["contrasts"]["judge"]["paired_inputs"] == 2
    assert report["contrasts"]["judge"]["change_percentage_points"] == 0.0
    assert report["contrasts_by_set"]["exploration"]["judge"]["paired_inputs"] == 0
    assert report["contrasts_by_round"]["2"]["judge"]["change_percentage_points"] == -100.0


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("factor", ["feedback_prompt", "judge_prompt"])
def test_confirmation_prefix_cold_resume_preserves_shared_draft_and_unknown_acceptance(
    tmp_path, dataset_path, runtime_config, exploration, uncertain, factor
):
    from test_real_experiment import _fixture_clients
    from test_real_generation import FakeClient

    from app.engine.real_confirmation import (
        confirmation_report,
        freeze_confirmation,
        run_confirmation,
    )
    from app.engine.real_experiment import record_smoke_review, run_experiment

    config = tmp_path / "smoke-config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    smoke = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="confirmed-smoke",
        seed=7,
        set_name="smoke",
    )
    run_experiment(
        tmp_path / "smoke.json", smoke, clients=_fixture_clients(smoke), trace_sink=lambda **_: None
    )
    record_smoke_review(smoke)
    selection = (
        choice(exploration)
        if factor == "feedback_prompt"
        else choice(exploration, "judge_prompt", "judge-system.st", "NEW JUDGE {{skill_md}}")
    )
    bundle = freeze_confirmation(exploration, selection, run_id="resume", seed=3)
    limit = 3 if factor == "feedback_prompt" else 2
    first = bundle["execution_plan"][0]
    case = next(
        case
        for case in exploration["frozen"]["dataset"]["sets"][first["set_name"]]
        if case["case_id"] == first["case_id"]
    )
    client_fixture = {
        **exploration,
        "frozen": {**exploration["frozen"], "dataset": {"sets": {"exploration": [case]}}},
        "execution_plan": [first],
    }
    clients = {version: _fixture_clients(client_fixture) for version in bundle["variants"]}
    if uncertain:
        clients = {
            version: {**roles, "generation": FakeClient([RuntimeError("unknown acceptance")])}
            for version, roles in clients.items()
        }
    output = tmp_path / "confirmation.json"
    run_confirmation(
        output, bundle, clients=clients, smoke=smoke, trace_sink=lambda **_: None, path_limit=limit
    )
    before = sum(len(client.requests) for roles in clients.values() for client in roles.values())
    assert sum(len(roles["generation"].requests) for roles in clients.values()) == 1
    saved = json.loads(output.read_text("utf-8"))
    assert all("provider_calls" not in run for run in saved["runs"].values())
    run_confirmation(
        output, saved, clients=clients, smoke=smoke, trace_sink=lambda **_: None, path_limit=limit
    )
    assert (
        sum(len(client.requests) for roles in clients.values() for client in roles.values())
        == before
    )
    report = confirmation_report(saved)
    assert report["planned_main_paths"] == 360
    assert report["physical"]["call_count"] == before
    if uncertain:
        assert before == 1
        assert report["evidence"]["stages"]["generation"]["unresolved"] == limit
        assert report["physical"]["cost"]["complete"] is False
    else:
        assert before == (10 if factor == "feedback_prompt" else 5)
        assert report["evidence"]["stages"]["generation"]["completed"] == limit
    cancelled = next(
        row
        for row in saved["runs"][first["run_key"]]["execution_plan"]
        if row["path_id"] == first["path_id"]
    )
    cancelled["state"] = "cancelled"
    run_confirmation(
        output, saved, clients=clients, smoke=smoke, trace_sink=lambda **_: None, path_limit=1
    )
    assert cancelled["state"] == "cancelled"


def test_confirmation_cli_no_change_never_constructs_provider_clients(
    tmp_path, exploration, monkeypatch
):
    import sys

    from scripts import structured_retry_eval as cli

    source, selection, output = (
        tmp_path / name for name in ("source.json", "decision.json", "confirmation.json")
    )
    source.write_text(json.dumps(exploration), "utf-8")
    selection.write_text(
        json.dumps({"decision": "no_change", "reason": "Evidence does not support a change"}),
        "utf-8",
    )
    monkeypatch.setattr(
        cli, "verified_clients", lambda *_: pytest.fail("No provider client permitted")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval",
            "freeze-confirmation",
            "--source",
            str(source),
            "--selection",
            str(selection),
            "--run-id",
            "cli-confirmation",
            "--seed",
            "2",
            "--output",
            str(output),
        ],
    )
    assert cli.main() == 0
    monkeypatch.setattr(sys, "argv", ["eval", "execute-confirmation", "--manifest", str(output)])
    assert cli.main() == 0
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval", "confirmation-evidence", "--manifest", str(output), "--output", str(report)],
    )
    assert cli.main() == 0
    assert json.loads(report.read_text("utf-8"))["planned_main_paths"] == 0
