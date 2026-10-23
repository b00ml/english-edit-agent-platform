"""The real-evaluation CLI freezes inputs and resumes durable local artifacts offline."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.engine import quality
from app.engine.evaluation_protocol import (
    freeze_protocol,
    load_dataset,
    resume_manifest,
    write_manifest,
)
from app.engine.reference_review import build_review_packets
from app.prompt_loader import build_user_prompt

BACKEND = Path(__file__).parents[1]


@pytest.mark.parametrize("role", ["judge", "reference"])
def test_execute_review_cli_runs_verified_frozen_service(
    tmp_path: Path,
    runtime_config: dict[str, Any],
    dataset_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
) -> None:
    from contextlib import nullcontext

    from test_real_generation import VALID, FakeClient

    from app import database
    from scripts import structured_retry_eval as cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config,
        backend=BACKEND,
        run_id="judge-cli",
        seed=21,
        set_name="smoke",
    )
    first = next(row for row in manifest["execution_plan"] if "single_choice" in row["case_id"])
    first["state"] = "completed"
    manifest["generation_results"] = {first["path_id"]: {"payload": VALID}}
    output = tmp_path / "run.json"
    write_manifest(output, manifest)
    responses = [
        {
            "dimension_scores": {
                rule["id"]: 90
                for rule in manifest["frozen"]["templates"][
                    next(
                        case["template_id"]
                        for case in manifest["frozen"]["dataset"]["sets"]["smoke"]
                        if case["case_id"] == first["case_id"]
                    )
                ]["quality_rules"]
            }
        }
    ]
    if role == "reference":
        from test_real_reference import review, solve

        responses = [solve(), review()]
    client = FakeClient(responses)
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext("fixture-session"))
    verified = []

    def clients(frozen: dict[str, Any], session: object) -> dict[str, FakeClient]:
        verified.append((frozen["frozen"]["config"]["profiles"][role], session))
        return {role: client}

    monkeypatch.setattr(cli, "verified_clients", clients)
    # Trace persistence is the external storage boundary; execute the real review service.
    from app.engine.real_judge import run_judge
    from app.engine.real_reference import run_reference_review

    monkeypatch.setattr(
        cli,
        "run_judge" if role == "judge" else "run_reference_review",
        lambda path, data, **kwargs: (run_judge if role == "judge" else run_reference_review)(
            path, data, trace_sink=lambda **row: None, **kwargs
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval",
            f"execute-{role}",
            "--manifest",
            str(output),
            "--dataset",
            str(dataset_path),
            "--config",
            str(config),
        ],
    )
    assert cli.main() == 0
    saved = json.loads(output.read_text("utf-8"))
    assert len(verified) == 1
    assert len(client.requests) == (1 if role == "judge" else 2)
    assert saved[f"{role}_results"][first["path_id"]]["state"] == "completed"
    accounting = json.loads(capsys.readouterr().out)
    denominator = (
        accounting["pass_rate_all_paths"]["denominator"]
        if role == "judge"
        else accounting["full_plan_denominator"]
    )
    assert denominator == 9


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    sets = {}
    for name, count in (("exploration", 10), ("confirmation", 10), ("smoke", 1)):
        cases = []
        for template in ("single_choice", "cloze", "reading"):
            for index in range(count):
                params = {
                    "grade": 10,
                    "difficulty": "中",
                    "quantity": 1,
                    "knowledge_point": hashlib.sha256(
                        f"{name}-{template}-{index}".encode()
                    ).hexdigest(),
                    "constraints": "test-only teaching requirement",
                }
                if template == "cloze":
                    params["blank_count"] = 5
                if template == "reading":
                    params["question_count"] = 3
                cases.append(
                    {
                        "case_id": f"{name}-{template}-{index}",
                        "source_family": f"test-only-{name}-{template}-{index}",
                        "teaching_goal": "test-only grammar target",
                        "scope": "test-only grade-ten scope",
                        "judgment_basis": "test-only rubric",
                        "template_id": template,
                        "params": params,
                    }
                )
        sets[name] = cases
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"version": "real-quality-cases-v1", "sets": sets}), "utf-8")
    return path


@pytest.fixture
def runtime_config() -> dict:
    return {
        "profiles": {
            role: {
                "name": role,
                "provider_id": "fixture-provider",
                "provider_config_hash": "fixture-provider-config-v1",
                "model_name": f"fixture-{role}",
                "model_family": family,
                "profile_hash": f"fixture-{role}-profile-v1",
            }
            for role, family in [
                ("generation", "family-a"),
                ("judge", "family-b"),
                ("reference", "family-c"),
            ]
        },
        "prices": {
            f"fixture-{role}": {
                "prompt_per_1k": 0.001,
                "completion_per_1k": 0.002,
                "currency": "fixture-currency",
                "verified": True,
            }
            for role in ("generation", "judge", "reference")
        },
        "generation": {
            "temperature": 0.7,
            "timeout_seconds": 120,
            "max_output_tokens": 2048,
            "context_limit_tokens": 8192,
        },
        "reference": {
            "temperature": 0.2,
            "timeout_seconds": 90,
            "max_output_tokens": 3072,
            "context_limit_tokens": 16384,
        },
        "judge": {
            "temperature": 0.3,
            "timeout_seconds": 60,
            "max_output_tokens": 1024,
            "context_limit_tokens": 8192,
            "sample_rounds": 1,
            "max_attempts": 3,
            "calibration": {},
        },
    }


@pytest.mark.parametrize(
    "calibration",
    [
        {"unknown_template": {}},
        {"single_choice": {"unknown_rule": 1.0}},
        {"single_choice": {"threshold": -1.0}},
        {"single_choice": {"threshold": 101.0}},
        {"single_choice": {"kp_match": float("inf")}},
        {"single_choice": {"kp_match": -1.0}},
        {
            "single_choice": {
                "kp_match": 0.0,
                "diff_match": 0.0,
                "distractor": 0.0,
                "unambiguous": 0.0,
            }
        },
    ],
)
def test_freeze_rejects_invalid_frozen_judge_calibration(
    tmp_path: Path,
    runtime_config: dict[str, Any],
    dataset_path: Path,
    calibration: dict[str, dict[str, float]],
) -> None:
    runtime_config["judge"]["calibration"] = calibration
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    with pytest.raises(ValueError, match="calibration"):
        freeze_protocol(
            dataset_path=dataset_path,
            config_path=config,
            backend=BACKEND,
            run_id="calibration-test",
            seed=21,
        )


def test_freeze_cli_persists_balanced_disjoint_protocol_and_blinded_contracts(
    tmp_path: Path, runtime_config: dict, dataset_path: Path
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), encoding="utf-8")
    output = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "scripts" / "structured_retry_eval.py"),
            "freeze",
            "--dataset",
            str(dataset_path),
            "--config",
            str(config),
            "--output",
            str(output),
            "--run-id",
            "fixture-run",
            "--seed",
            "21",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["version"] == "real-quality-manifest-v1"
    assert manifest["run_id"] == "fixture-run"
    assert manifest["provider_calls"] == []
    dataset = manifest["frozen"]["dataset"]
    assert {name: len(cases) for name, cases in dataset["sets"].items()} == {
        "exploration": 30,
        "confirmation": 30,
        "smoke": 3,
    }
    all_cases = [case for cases in dataset["sets"].values() for case in cases]
    assert len({case["case_id"] for case in all_cases}) == 63
    assert len({json.dumps(case["params"], sort_keys=True) for case in all_cases}) == 63
    for name, cases in dataset["sets"].items():
        assert {
            template: sum(case["template_id"] == template for case in cases)
            for template in ("single_choice", "cloze", "reading")
        } == dict.fromkeys(("single_choice", "cloze", "reading"), 1 if name == "smoke" else 10)
        for case in cases:
            assert case["params"]["grade"] == 10
            assert case["params"]["difficulty"] == "中"
            assert case["params"]["quantity"] == 1
            assert case["params"]["knowledge_point"]
            assert case["params"]["constraints"]
            assert case["params"].get("blank_count", 5) == 5
            assert case["params"].get("question_count", 3) == 3
    assert set(manifest["identities"]) == {
        "dataset",
        "templates",
        "prompts",
        "skills",
        "config",
        "execution_plan",
    }
    assert len(manifest["execution_plan"]) == 90
    assert {path["arm"] for path in manifest["execution_plan"]} == {
        "single_attempt",
        "blind_retry",
        "feedback_retry",
    }
    assert {path["set_name"] for path in manifest["execution_plan"]} == {"exploration"}
    assert all(path["state"] == "planned" for path in manifest["execution_plan"])
    assert len({path["first_draft_id"] for path in manifest["execution_plan"]}) == 30
    assert "api_key" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "category", ["dataset", "templates", "prompts", "skills", "config", "execution_plan"]
)
def test_resume_rejects_each_frozen_identity_drift(
    tmp_path: Path, runtime_config: dict, dataset_path: Path, category: str
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config_path,
        backend=BACKEND,
        run_id="resume-test",
        seed=21,
    )
    manifest_path = tmp_path / "manifest.json"
    if category == "execution_plan":
        manifest["execution_plan"][0]["arm"] = "tampered"
    elif category == "config":
        runtime_config["generation"]["temperature"] = 0.1
        config_path.write_text(json.dumps(runtime_config), "utf-8")
    else:
        manifest["frozen"][category]["tampered"] = "changed"
    write_manifest(manifest_path, manifest)
    with pytest.raises(ValueError, match="drift"):
        resume_manifest(
            manifest_path=manifest_path,
            dataset_path=dataset_path,
            config_path=config_path,
            backend=BACKEND,
        )


def test_resume_preserves_unresolved_and_inflight_evidence_without_replay(
    tmp_path: Path, runtime_config: dict, dataset_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(runtime_config), "utf-8")
    manifest = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config_path,
        backend=BACKEND,
        run_id="resume-test",
        seed=21,
    )
    manifest["execution_plan"][0]["state"] = "in_flight"
    manifest["execution_plan"][1]["state"] = "unresolved"
    manifest["execution_plan"][2]["state"] = "completed"
    manifest["execution_plan"][3]["state"] = "cancelled"
    manifest["provider_calls"] = [
        {
            "call_id": "existing-call",
            "state": "unresolved",
            "reason": "provider acceptance unknown",
            "trace_id": "trace-1",
        }
    ]
    manifest["artifacts"] = [{"path": "local-evidence.json", "sha256": "retained"}]
    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest_path, manifest)
    before = manifest_path.read_bytes()
    resumed = resume_manifest(
        manifest_path=manifest_path,
        dataset_path=dataset_path,
        config_path=config_path,
        backend=BACKEND,
    )
    assert resumed == manifest
    assert manifest_path.read_bytes() == before
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "scripts" / "structured_retry_eval.py"),
            "resume",
            "--manifest",
            str(manifest_path),
            "--dataset",
            str(dataset_path),
            "--config",
            str(config_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["provider_calls_replayed"] == 0
    assert manifest_path.read_bytes() == before


def test_empty_official_dataset_requires_population_and_never_creates_output(
    tmp_path: Path, runtime_config: dict
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(runtime_config), "utf-8")
    output = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND / "scripts" / "structured_retry_eval.py"),
            "freeze",
            "--dataset",
            str(BACKEND / "evaluation" / "real_quality_cases.json"),
            "--config",
            str(config_path),
            "--output",
            str(output),
            "--seed",
            "21",
            "--run-id",
            "empty",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Populate exploration: expected 30 cases, found 0" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "change", ["duplicate", "geometry", "secret", "reference_family", "judge_family"]
)
def test_freeze_rejects_unsafe_or_incompatible_inputs(
    tmp_path: Path, runtime_config: dict, dataset_path: Path, change: str
) -> None:
    cases = json.loads(dataset_path.read_text("utf-8"))
    if change == "duplicate":
        cases["sets"]["confirmation"][0] = cases["sets"]["exploration"][0]
    elif change == "geometry":
        cases["sets"]["exploration"][0]["template_id"] = "reading"
    elif change == "secret":
        runtime_config["profiles"]["generation"]["api_key"] = "test-only-secret"
    elif change == "reference_family":
        runtime_config["profiles"]["reference"]["model_family"] = "family-a"
    else:
        runtime_config["profiles"]["reference"]["model_family"] = "family-b"
    dataset_path.write_text(json.dumps(cases), "utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(runtime_config), "utf-8")
    with pytest.raises(ValueError):
        freeze_protocol(
            dataset_path=dataset_path,
            config_path=config_path,
            backend=BACKEND,
            run_id="bad-input",
            seed=21,
        )


def test_teaching_context_reaches_generator_judge_and_blinded_reference(
    dataset_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = json.loads(dataset_path.read_text("utf-8"))["sets"]["exploration"][0]
    template = SimpleNamespace(
        **yaml.safe_load((BACKEND / "app" / "templates" / "single_choice.yaml").read_text("utf-8"))
    )
    generated_prompt = build_user_prompt(template, case["params"])
    assert "10" in generated_prompt and case["params"]["constraints"] in generated_prompt
    captured = []

    def create(**kwargs: object) -> SimpleNamespace:
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "dimension_scores": {
                                    rule["id"]: 80.0 for rule in template.quality_rules
                                }
                            }
                        )
                    )
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(
        quality,
        "_get_openai_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    monkeypatch.setattr(quality, "record_trace", lambda **kwargs: None)
    payload = {"stem": "test-only", "answer": "A", "explanation": "test-only"}
    quality.run_quality_check(
        template, payload, model_name="test-only", rounds=1, generation_params=case["params"]
    )
    assert case["params"]["constraints"] in str(captured[0]["messages"])
    packets = build_review_packets(
        {
            "hidden-arm": {
                "outcomes": [
                    {"case_id": case["case_id"], "payload": payload, "final_schema_success": True}
                ]
            }
        },
        case_contexts={case["case_id"]: case["params"]},
    )
    assert packets[0]["generation_constraints"] == case["params"]
    assert "hidden-arm" not in json.dumps(packets)


@pytest.mark.parametrize("role", ["generation", "judge", "reference"])
@pytest.mark.parametrize(
    "field", ["timeout_seconds", "max_output_tokens", "context_limit_tokens", "temperature"]
)
def test_freeze_requires_explicit_request_limits_for_every_role(
    tmp_path: Path, runtime_config: dict[str, Any], dataset_path: Path, role: str, field: str
) -> None:
    runtime_config[role].pop(field, None)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    with pytest.raises(ValueError, match=field):
        freeze_protocol(
            dataset_path=dataset_path,
            config_path=config,
            backend=BACKEND,
            run_id="missing-request-limit",
            seed=21,
        )


@pytest.mark.parametrize("field", ["source_family", "teaching_goal", "scope", "judgment_basis"])
def test_dataset_requires_explicit_teaching_and_source_metadata(
    dataset_path: Path, field: str
) -> None:
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["sets"]["exploration"][0].pop(field, None)
    dataset_path.write_text(json.dumps(dataset), "utf-8")
    with pytest.raises(ValueError, match=field):
        load_dataset(dataset_path, BACKEND)


@pytest.mark.parametrize(
    "mutation", ["source", "source_unicode", "unicode", "edit", "long_edit", "id_only"]
)
def test_dataset_rejects_source_and_near_duplicate_leaks(dataset_path: Path, mutation: str) -> None:
    dataset = json.loads(dataset_path.read_text("utf-8"))
    original = dataset["sets"]["exploration"][0]
    other = dataset["sets"]["confirmation"][0]
    if mutation.startswith("source"):
        family = original["source_family"]
        other["source_family"] = (
            "".join(chr(ord(char) + 0xFEE0) for char in family)
            if mutation == "source_unicode"
            else family
        )
        error = "source famil"
    else:
        if mutation == "long_edit":
            original["params"]["knowledge_point"] = "test-only reading explanation " * 30
        other["params"] = dict(original["params"])
        point = original["params"]["knowledge_point"]
        if mutation == "unicode":
            other["params"]["knowledge_point"] = "".join(chr(ord(char) + 0xFEE0) for char in point)
        elif mutation == "long_edit":
            other["params"]["knowledge_point"] = "changed " + point[8:]
        elif mutation == "edit":
            other["params"]["knowledge_point"] = point[:-1] + "xyz"
        error = "disjoint" if mutation == "id_only" else "near-duplicate"
    dataset_path.write_text(json.dumps(dataset), "utf-8")
    with pytest.raises(ValueError, match=error):
        load_dataset(dataset_path, BACKEND)


@pytest.mark.parametrize("role", ["generation", "judge", "reference"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("max_output_tokens", 0),
        ("context_limit_tokens", 1),
        ("timeout_seconds", 0),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("max_output_tokens", 1.5),
    ],
)
def test_freeze_rejects_invalid_or_incompatible_role_limits(
    tmp_path: Path,
    runtime_config: dict[str, Any],
    dataset_path: Path,
    role: str,
    field: str,
    value: float,
) -> None:
    runtime_config[role][field] = value
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    with pytest.raises(ValueError, match=field):
        freeze_protocol(
            dataset_path=dataset_path,
            config_path=config,
            backend=BACKEND,
            run_id="invalid-request-limit",
            seed=21,
        )


@pytest.mark.parametrize("field", ["source_family", "teaching_goal", "scope", "judgment_basis"])
def test_dataset_rejects_blank_case_metadata(dataset_path: Path, field: str) -> None:
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["sets"]["exploration"][0][field] = "  \t\n"
    dataset_path.write_text(json.dumps(dataset), "utf-8")
    with pytest.raises(ValueError, match=field):
        load_dataset(dataset_path, BACKEND)


@pytest.mark.parametrize("field", ["prompt_per_1k", "completion_per_1k"])
def test_freeze_requires_explicit_price_or_unknown_value(
    tmp_path: Path, runtime_config: dict[str, Any], dataset_path: Path, field: str
) -> None:
    runtime_config["prices"]["fixture-generation"].pop(field)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(runtime_config), "utf-8")
    with pytest.raises(ValueError, match=field):
        freeze_protocol(
            dataset_path=dataset_path,
            config_path=config,
            backend=BACKEND,
            run_id="missing-price",
            seed=21,
        )
