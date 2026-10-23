"""Fresh disagreement review and export through durable artifacts; fake provider only."""

import copy
import json
from pathlib import Path

import pytest
from test_real_generation import VALID, FakeClient
from test_real_reference import review, solve

from app.engine.evaluation_protocol import freeze_protocol

pytest_plugins = ("test_real_quality_protocol",)


@pytest.fixture
def source(tmp_path, dataset_path, runtime_config):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=Path(__file__).parents[1],
        run_id="initial",
        seed=4,
    )
    for stage in ("generation", "judge", "reference"):
        manifest[f"{stage}_results"] = {}
    for row in manifest["execution_plan"]:
        if "single_choice" not in row["case_id"]:
            continue
        row["state"] = "completed"
        manifest["generation_results"][row["path_id"]] = {"payload": copy.deepcopy(VALID)}
        manifest["judge_results"][row["path_id"]] = {"state": "completed", "passed": True}
        manifest["reference_results"][row["path_id"]] = {
            "state": "completed",
            "reference_pass": not row["case_id"].endswith(("-0", "-1")),
        }
    return manifest


def test_selection_all_disagreements_random_six_context_dedup_and_immutable_no_change(source):
    from app.engine.real_confirmation import freeze_confirmation
    from app.engine.real_rereview import freeze_rereview, validate_rereview

    original = copy.deepcopy(source)
    no_change = freeze_confirmation(
        source,
        {"decision": "no_change", "reason": "No reproducible defect."},
        run_id="confirmation",
        seed=4,
    )
    bundle = freeze_rereview(no_change, run_id="fresh", seed=27)
    assert len(bundle["pool"]) == 10  # Same payload, ten different teaching contexts.
    selected = bundle["selection"]
    assert len(selected) == 7
    assert sum("disagreement" in row["reasons"] for row in selected) == 2
    assert sum("random" in row["reasons"] for row in selected) == 6
    assert sum(set(row["reasons"]) == {"disagreement", "random"} for row in selected) == 1
    assert len(bundle["reviews"]) == len({row["content_id"] for row in selected}) == 7
    assert all(len(row["memberships"]) == 3 for row in selected)
    assert bundle == freeze_rereview(no_change, run_id="fresh", seed=27)
    assert source == original
    assert bundle["original"]["selection"]["decision"] == "no_change"
    altered = copy.deepcopy(bundle)
    altered["reviews"][selected[0]["content_id"]]["reference_teaching_context"][
        "judgment_basis"
    ] = "changed after freeze"
    with pytest.raises(ValueError, match="immutable"):
        validate_rereview(altered)
    bundle["selection"][0]["reasons"].append("changed")
    with pytest.raises(ValueError, match="immutable"):
        validate_rereview(bundle)


def test_fresh_two_phase_once_preserves_originals_and_unknown_after_cold_resume(source, tmp_path):
    from app.engine.real_rereview import freeze_rereview, run_rereview

    original = copy.deepcopy(source)
    bundle = freeze_rereview(source, run_id="fresh", seed=27)
    client = FakeClient(
        [TimeoutError(), *[response for _ in range(6) for response in (solve(), review())]]
    )
    artifact = tmp_path / "fresh.json"
    traces = []
    run_rereview(
        artifact,
        bundle,
        clients=lambda run: {"reference": client},
        trace_sink=lambda **row: traces.append(row),
    )
    assert len(client.requests) == 13
    assert source == original == bundle["original"]
    serialized = json.dumps(client.requests)
    for forbidden in (
        "judge_pass",
        "reference_pass",
        "disagreement",
        "blind_retry",
        "feedback_retry",
        "single_attempt",
        "memberships",
        "initial",
    ):
        assert forbidden not in serialized
    first_solve = json.loads(client.requests[1]["messages"][1]["content"].split("\n", 1)[1])
    assert first_solve["teaching_context"] == {
        "teaching_goal": "test-only grammar target",
        "scope": "test-only grade-ten scope",
        "judgment_basis": "test-only rubric",
        "grade": 10,
        "difficulty": "中",
        "knowledge_point": source["frozen"]["dataset"]["sets"]["exploration"][0]["params"][
            "knowledge_point"
        ],
        "quantity": 1,
        "constraints": "test-only teaching requirement",
    }
    assert "answer" not in json.dumps(first_solve)
    assert "explanation" not in json.dumps(first_solve)
    assert len({row["call_id"] for row in bundle["provider_calls"]}) == 13
    assert len(traces) == 13
    saved = json.loads(artifact.read_text("utf-8"))
    assert all("provider_calls" not in run for run in saved["reviews"].values())
    before = copy.deepcopy(saved)
    run_rereview(artifact, saved, clients=lambda run: pytest.fail("No second context allowed"))
    assert saved["provider_calls"] == before["provider_calls"]
    outcomes = [next(iter(run["reference_results"].values())) for run in saved["reviews"].values()]
    assert sum(row["state"] == "unresolved" for row in outcomes) == 1
    assert sum(row["state"] == "completed" for row in outcomes) == 6


def test_evidence_all_states_honest_model_agreement_whitelist_and_zero_change(source, tmp_path):
    from app.engine.real_confirmation import freeze_confirmation
    from app.engine.real_rereview import build_evidence_package, freeze_rereview, run_rereview

    marker = "PRIVATE-topic-prompt-endpoint-source-secret"
    source["private_note"] = marker
    # Original outcomes can legitimately be unresolved; they cannot enter decisive comparisons.
    first = source["execution_plan"][0]["path_id"]
    source["reference_results"][first] = {"state": "unresolved", "error": marker}
    original = freeze_confirmation(
        source, {"decision": "no_change", "reason": marker}, run_id="no-change", seed=9
    )
    bundle = freeze_rereview(original, run_id="fresh", seed=27)
    initial = build_evidence_package(bundle)
    assert initial["local"]["confirmation"]["planned_main_paths"] == 0
    assert initial["local"]["sampling"]["selected"] == 7
    assert initial["local"]["sampling"]["overlap"] == 1
    assert initial["local"]["rereview_states"]["missing"] == 7
    assert initial["public_candidate"]["publication"] == "requires_separate_approval"
    assert marker in json.dumps(initial["local"])
    assert marker not in json.dumps(initial["public_candidate"])
    assert "人工准确率" in initial["summary_zh"]
    assert "用户决策" in initial["summary_zh"] and "AI 实现" in initial["summary_zh"]
    assert "自动化执行" in initial["summary_zh"] and "模型判断" in initial["summary_zh"]
    client = FakeClient(
        [TimeoutError(), *[response for _ in range(6) for response in (solve(), review())]]
    )
    run_rereview(
        tmp_path / "fresh.json",
        bundle,
        clients=lambda run: {"reference": client},
        trace_sink=lambda **row: None,
    )
    report = build_evidence_package(bundle)
    assert report["local"]["rereview_states"]["unresolved"] == 1
    assert report["local"]["rereview_physical"]["cost"]["total"] is None
    assert report["local"]["rereview_physical"]["cost"]["denominator"] == 13
    agreement = report["local"]["comparisons"]["judge_rereview"]
    assert agreement["paired"] == 18 and agreement["planned"] == 21
    assert agreement["kappa"] is None
    assert agreement["kappa_status"] == "unavailable_degenerate_marginals"
    assert marker not in json.dumps(report["public_candidate"])


def test_cli_freezes_executes_fake_reference_and_exports_local_files(source, tmp_path, monkeypatch):
    import sys

    from app import database
    from app.engine import real_experiment
    from scripts import structured_retry_eval as cli

    original = tmp_path / "source.json"
    original.write_text(json.dumps(source), "utf-8")
    manifest = tmp_path / "rereview.json"
    report = tmp_path / "evidence.json"
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"execution_plan": [], "protocol": {"set_name": "smoke"}}), "utf-8")

    def command(*args):
        monkeypatch.setattr(sys, "argv", ["eval", *map(str, args)])
        return cli.main()

    monkeypatch.setattr(
        cli, "verified_clients", lambda *_: pytest.fail("No client before execution")
    )
    assert (
        command(
            "freeze-rereview",
            "--source",
            original,
            "--run-id",
            "cli-fresh",
            "--seed",
            27,
            "--output",
            manifest,
        )
        == 0
    )
    with pytest.raises(SystemExit):
        command("execute-rereview", "--manifest", manifest, "--reviewed-smoke", smoke)

    # The provider/DB boundary is replaced; the real freeze, runner, persistence and exporter run.
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    client = FakeClient([response for _ in range(7) for response in (solve(), review())])
    monkeypatch.setattr(database, "SessionLocal", FakeSession)
    monkeypatch.setattr(cli, "verified_clients", lambda *_: {"reference": client})
    monkeypatch.setattr(real_experiment, "require_reviewed_smoke", lambda *_: None)
    # Avoid the default Trace sink connecting to a real database; ledger traces remain explicit.
    import app.engine.real_rereview as rereview

    actual = rereview.run_rereview
    monkeypatch.setattr(
        rereview,
        "run_rereview",
        lambda *args, **kwargs: actual(*args, **kwargs, trace_sink=lambda **_: True),
    )
    assert command("execute-rereview", "--manifest", manifest, "--reviewed-smoke", smoke) == 0
    assert len(client.requests) == 14
    assert command("evidence-package", "--manifest", manifest, "--output", report) == 0
    assert json.loads(report.read_text("utf-8"))["rereview_states"]["completed"] == 7
    assert "模型判断" in report.with_suffix(".md").read_text("utf-8")
    public = json.loads(report.with_name("evidence.public-candidate.json").read_text("utf-8"))
    assert public["publication"] == "requires_separate_approval"
