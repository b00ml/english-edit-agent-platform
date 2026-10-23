"""Offline smoke/exploration orchestration and artifact accounting at the public seam."""

import json

import pytest
from test_real_quality_protocol import BACKEND

pytest_plugins = ("test_real_quality_protocol",)

from app.engine.evaluation_protocol import (
    freeze_protocol,
    resume_manifest,
    write_manifest,
)


def test_caps_count_every_transport_send_across_resume(tmp_path):
    from test_real_generation import VALID, FakeClient, local_manifest

    from app.engine.real_evaluation import EvaluationLedger
    from app.errors import EvaluationTransportError

    manifest = local_manifest()
    manifest["protocol"] = {"call_limits": {"generate": {"semantic": 2, "physical": 2}}}
    path = tmp_path / "run.json"
    client = FakeClient(
        [
            EvaluationTransportError(accepted=False, completed=False, transient=True),
            EvaluationTransportError(accepted=False, completed=False, transient=True),
            VALID,
        ]
    )
    ledger = EvaluationLedger(path, manifest, trace_sink=lambda **_: None)
    kwargs = {
        "stage": "generate",
        "model": "fixture-model",
        "price": manifest["frozen"]["config"]["prices"]["fixture-model"],
        "invoke": lambda: client.create(messages=[]),
    }
    with pytest.raises(ValueError, match="physical call limit"):
        ledger.call("first", [], **kwargs)
    assert len(client.requests) == 2
    saved = json.loads(path.read_text("utf-8"))
    with pytest.raises(ValueError, match="physical call limit"):
        EvaluationLedger(path, saved).call("second", [], **kwargs)
    assert len(client.requests) == 2


def test_smoke_is_frozen_separately_with_derived_call_limits(
    tmp_path, dataset_path, runtime_config
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    smoke = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="smoke-test",
        seed=19,
        set_name="smoke",
    )
    assert len(smoke["execution_plan"]) == 9
    assert {p["set_name"] for p in smoke["execution_plan"]} == {"smoke"}
    assert smoke["protocol"]["call_limits"]["generate"] == {"semantic": 15, "physical": 45}
    assert smoke["protocol"]["call_limits"]["reference_solve"] == {
        "semantic": 18,
        "physical": 54,
    }
    output = tmp_path / "smoke.json"
    write_manifest(output, smoke)
    resumed = resume_manifest(
        manifest_path=output,
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
    )
    assert resumed == smoke
    exploration = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="exploration-test",
        seed=19,
    )
    assert len(exploration["execution_plan"]) == 90
    assert exploration["protocol"]["call_limits"]["generate"] == {
        "semantic": 150,
        "physical": 450,
    }


def test_exploration_cannot_send_without_executed_reviewed_matching_smoke(tmp_path):
    from test_real_generation import FakeClient, local_manifest

    from app.engine.real_experiment import record_smoke_review, run_experiment

    manifest = local_manifest()
    manifest["protocol"]["set_name"] = "exploration"
    clients = {role: FakeClient([]) for role in ("generation", "judge", "reference")}
    with pytest.raises(ValueError, match="reviewed smoke"):
        run_experiment(tmp_path / "run.json", manifest, clients=clients)
    smoke = json.loads(json.dumps(manifest))
    smoke["protocol"]["set_name"] = "smoke"
    with pytest.raises(ValueError, match="complete"):
        record_smoke_review(smoke)
    assert all(not client.requests for client in clients.values())


def test_report_retains_full_plan_unknown_cost_and_separate_logical_membership():
    from test_real_generation import local_manifest

    from app.engine.real_experiment import build_evidence_report, interview_brief

    manifest = local_manifest()
    manifest["execution_plan"][0]["state"] = "completed"
    manifest["generation_results"] = {
        "single_attempt": {
            "attempts": [{"source_call_id": "shared"}],
            "payload": {"fixture": True},
        },
        "blind_retry": {"attempts": [{"source_call_id": "shared"}]},
    }
    manifest["provider_calls"] = [
        {
            "call_id": "shared",
            "request_id": "shared",
            "stage": "generate",
            "state": "completed",
            "cost": None,
            "usage": None,
            "trace_state": "failed",
            "latency_ms": 12,
        }
    ]
    report = build_evidence_report(manifest)
    counts = report["stages"]["generation"]
    assert counts["planned"] == 3 and counts["success"] == 1
    assert counts["submitted"] == 2 and counts["missing"] == 2
    assert report["physical"]["call_count"] == 1
    assert report["physical"]["cost"]["total"] is None
    assert report["physical"]["cost"]["known_subtotal"] == 0
    assert report["arms"]["blind_retry"]["logical"]["call_count"] == 1
    assert report["arms"]["single_attempt"]["logical"]["call_count"] == 1
    assert report["unknown_cost_call_ids"] == ["shared"]
    assert "人工金标准" in interview_brief(report)


def test_cli_exports_partial_local_evidence_without_connecting(
    tmp_path, dataset_path, runtime_config, monkeypatch
):
    import sys

    from scripts import structured_retry_eval as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path, config_path=config, backend=BACKEND, run_id="partial", seed=2
    )
    path = tmp_path / "run.json"
    write_manifest(path, manifest)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval",
            "evidence",
            "--manifest",
            str(path),
            "--dataset",
            str(dataset_path),
            "--config",
            str(config),
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(
        cli, "verified_clients", lambda *_: pytest.fail("artifact report must not connect")
    )
    assert cli.main() == 0
    report = json.loads(output.read_text("utf-8"))
    assert report["stages"]["generation"]["missing"] == 90
    assert output.with_suffix(".md").is_file()


def _fixture_clients(manifest):
    import copy

    from test_real_generation import VALID, FakeClient
    from test_real_reference import review, solve

    cases = {
        row["case_id"]: row
        for row in manifest["frozen"]["dataset"]["sets"][manifest["protocol"]["set_name"]]
    }
    case_ids = list(dict.fromkeys(row["case_id"] for row in manifest["execution_plan"]))
    drafts, scores, references = [], [], []
    for case_id in case_ids:
        template_id = cases[case_id]["template_id"]
        count = {"single_choice": 1, "cloze": 5, "reading": 3}[template_id]
        passage = (
            "An offline test passage." if template_id == "reading" else " ____ ".join(["word"] * 6)
        )
        payload = (
            copy.deepcopy(VALID)
            if count == 1
            else {
                "passage": passage,
                "blanks" if template_id == "cloze" else "questions": [
                    {**copy.deepcopy(VALID), "index": i} for i in range(1, count + 1)
                ],
            }
        )
        drafts.append(payload)
        scores.append(
            {
                "dimension_scores": {
                    rule["id"]: 90
                    for rule in manifest["frozen"]["templates"][template_id]["quality_rules"]
                }
            }
        )
        references.extend([solve(count), review(count)])
    return {
        "generation": FakeClient(drafts),
        "judge": FakeClient(scores),
        "reference": FakeClient(references),
    }


def test_full_smoke_review_then_exploration_resume_without_duplicate_calls(
    tmp_path, dataset_path, runtime_config
):

    from app.engine.real_experiment import (
        build_evidence_report,
        record_smoke_review,
        require_reviewed_smoke,
        run_experiment,
    )

    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="full-smoke",
        seed=7,
        set_name="smoke",
    )
    clients = _fixture_clients(manifest)
    path = tmp_path / "smoke.json"
    run_experiment(path, manifest, clients=clients, trace_sink=lambda **_: None)
    assert len(clients["generation"].requests) == 3
    assert build_evidence_report(manifest)["stages"]["reference"]["success"] == 9
    record_smoke_review(manifest)
    write_manifest(path, manifest, overwrite=True)
    frozen = resume_manifest(
        manifest_path=path, dataset_path=dataset_path, config_path=config, backend=BACKEND
    )
    run_experiment(path, frozen, clients=clients, trace_sink=lambda **_: None)
    assert sum(len(c.requests) for c in clients.values()) == 12
    exploration = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="full-exploration",
        seed=7,
    )
    require_reviewed_smoke(exploration, frozen)
    exploration_clients = _fixture_clients(exploration)
    run_experiment(
        tmp_path / "exploration.json",
        exploration,
        clients=exploration_clients,
        smoke=frozen,
        trace_sink=lambda **_: None,
    )
    run_experiment(
        tmp_path / "exploration.json",
        exploration,
        clients=exploration_clients,
        smoke=frozen,
        trace_sink=lambda **_: None,
    )
    report = build_evidence_report(exploration)
    assert report["stages"]["reference"]["success"] == report["planned_paths"] == 90
    assert report["model_agreement"]["paired"] == 30
    assert report["model_agreement"]["paired_paths"] == 90
    assert report["physical"]["call_count"] == 120
    assert sum(arm["logical"]["call_count"] for arm in report["arms"].values()) == 360
    assert sum(len(c.requests) for c in exploration_clients.values()) == 120
    exploration["identities"]["dataset"] = "drift"
    with pytest.raises(ValueError, match="match"):
        require_reviewed_smoke(exploration, frozen)
    frozen["provider_calls"][0]["trace_state"] = "failed"
    with pytest.raises(ValueError, match="complete"):
        require_reviewed_smoke(exploration, frozen)


def test_semantic_cap_reuses_completed_response_but_blocks_new_send(tmp_path):
    from test_real_generation import VALID, FakeClient, local_manifest

    from app.engine.real_evaluation import EvaluationLedger

    manifest = local_manifest()
    manifest["protocol"]["call_limits"]["generate"] = {"semantic": 1, "physical": 3}
    client = FakeClient([VALID, VALID])
    ledger = EvaluationLedger(tmp_path / "run.json", manifest, trace_sink=lambda **_: None)
    kwargs = {
        "stage": "generate",
        "model": "fixture-model",
        "price": manifest["frozen"]["config"]["prices"]["fixture-model"],
        "invoke": lambda: client.create(messages=[]),
    }
    ledger.call("first", [], **kwargs)
    assert ledger.call("first", [], **kwargs).replayed is True
    with pytest.raises(ValueError, match="semantic call limit"):
        ledger.call("second", [], **kwargs)
    assert len(client.requests) == 1


def test_report_ignores_unsent_attempts_and_keeps_trace_gap_cost_incomplete():
    from test_real_generation import local_manifest

    from app.engine.real_experiment import build_evidence_report

    manifest = local_manifest()
    manifest["generation_results"] = {
        "single_attempt": {"attempts": [{"source_call_id": "unsent"}]}
    }
    manifest["provider_calls"] = [
        {
            "call_id": "other",
            "stage": "generate",
            "cost": 0.2,
            "currency": "USD",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "trace_state": "failed",
            "latency_ms": 1,
        }
    ]
    report = build_evidence_report(manifest)
    assert report["stages"]["generation"]["submitted"] == 0
    assert report["physical"]["cost"]["known_subtotal"] == 0.2
    assert report["physical"]["cost"]["complete"] is False
    assert report["physical"]["cost"]["total"] is None
