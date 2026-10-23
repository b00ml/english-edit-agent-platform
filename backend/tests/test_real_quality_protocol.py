"""The real-evaluation CLI freezes inputs and resumes durable local artifacts offline."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.engine import quality
from app.engine.evaluation_protocol import freeze_protocol, resume_manifest, write_manifest
from app.engine.reference_review import build_review_packets
from app.prompt_loader import build_user_prompt

BACKEND = Path(__file__).parents[1]


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
                    "knowledge_point": f"test-only {name} {template} {index}",
                    "constraints": "test-only teaching requirement",
                }
                if template == "cloze":
                    params["blank_count"] = 5
                if template == "reading":
                    params["question_count"] = 3
                cases.append(
                    {
                        "case_id": f"{name}-{template}-{index}",
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
        "generation": {"temperature": 0.7, "timeout_seconds": 120},
        "judge": {
            "temperature": 0.3,
            "sample_rounds": 1,
            "max_attempts": 3,
            "calibration": {},
        },
    }


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
