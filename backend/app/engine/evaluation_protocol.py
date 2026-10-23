"""Freeze local evaluation inputs and reject incompatible resumptions without model calls."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import validate
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field

TEMPLATES = ("single_choice", "cloze", "reading")
ARMS = ("single_attempt", "blind_retry", "feedback_retry")
MANIFEST_VERSION = "real-quality-manifest-v1"


class ProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_config_hash: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_family: str = Field(min_length=1)
    profile_hash: str = Field(min_length=1)


class PriceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt_per_1k: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    completion_per_1k: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=1)
    verified: bool


class GenerationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    temperature: float = Field(ge=0, le=2)
    timeout_seconds: int = Field(gt=0)


class JudgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    temperature: float = Field(ge=0, le=2)
    sample_rounds: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    calibration: dict[str, dict[str, float]]


class RuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profiles: dict[Literal["generation", "judge", "reference"], ProfileSnapshot]
    prices: dict[str, PriceSnapshot]
    generation: GenerationSnapshot
    judge: JudgeSnapshot


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def judge_policies(
    templates: dict[str, Any], calibration: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """Resolve frozen rule weights and reserved threshold without live calibration reads."""
    if set(calibration) - templates.keys():
        raise ValueError("Judge calibration contains unknown templates")
    policies = {}
    for template_id, template in templates.items():
        overrides = calibration.get(template_id, {})
        rules = template["quality_rules"]
        ids = {rule["id"] for rule in rules}
        if set(overrides) - (ids | {"threshold"}):
            raise ValueError("Judge calibration contains undeclared dimensions")
        weights = {rule["id"]: overrides.get(rule["id"], rule["weight"]) for rule in rules}
        threshold = overrides.get("threshold")
        if threshold is None:
            threshold = template.get("run_config", {}).get("quality_threshold", 70)
        if (
            not math.isfinite(threshold)
            or not 0 <= threshold <= 100
            or any(not math.isfinite(value) or value < 0 for value in weights.values())
            or sum(weights.values()) <= 0
        ):
            raise ValueError(
                "Judge calibration requires finite nonnegative weights, positive total "
                "and a threshold in 0..100"
            )
        policies[template_id] = {"weights": weights, "threshold": threshold}
    return policies


def load_dataset(path: Path, backend: Path) -> dict[str, Any]:
    """Validate populated, non-overlapping sets against the frozen protocol geometry."""
    dataset: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(
        (backend / "evaluation" / "real_quality_cases.schema.json").read_text("utf-8")
    )
    try:
        validate(dataset, schema)
    except SchemaValidationError as exc:
        raise ValueError(
            f"Invalid evaluation dataset at {list(exc.absolute_path)}: {exc.message}"
        ) from exc
    identities: set[str] = set()
    inputs: set[str] = set()
    for set_name, size in (("exploration", 30), ("confirmation", 30), ("smoke", 3)):
        cases = dataset["sets"][set_name]
        if len(cases) != size:
            raise ValueError(f"Populate {set_name}: expected {size} cases, found {len(cases)}")
        for template_id in TEMPLATES:
            if sum(case["template_id"] == template_id for case in cases) != size // 3:
                raise ValueError(f"{set_name} must be balanced across all three templates")
        for case in cases:
            input_id = canonical_hash(
                {"template_id": case["template_id"], "params": case["params"]}
            )
            if case["case_id"] in identities or input_id in inputs:
                raise ValueError("All case identifiers and inputs must be disjoint across sets")
            identities.add(case["case_id"])
            inputs.add(input_id)
            params = case["params"]
            if case["template_id"] == "cloze" and params.get("blank_count") != 5:
                raise ValueError("Cloze cases require exactly five blanks")
            if case["template_id"] == "reading" and params.get("question_count") != 3:
                raise ValueError("Reading cases require exactly three questions")
    return dataset


def asset_snapshot(backend: Path) -> dict[str, Any]:
    """Capture content as well as hashes so the complete protocol remains replayable."""
    templates = {
        name: yaml.safe_load((backend / "app" / "templates" / f"{name}.yaml").read_text("utf-8"))
        for name in TEMPLATES
    }
    prompts = {
        path.name: path.read_text("utf-8") for path in sorted((backend / "prompts").glob("*.st"))
    }
    skills = {
        path.relative_to(backend).as_posix(): path.read_text("utf-8")
        for path in sorted((backend / "skills").rglob("*"))
        if path.is_file() and path.name in {"SKILL.md", "skill.meta.yml"}
    }
    return {"templates": templates, "prompts": prompts, "skills": skills}


def freeze_protocol(
    *, dataset_path: Path, config_path: Path, backend: Path, run_id: str, seed: int
) -> dict[str, Any]:
    """Build an offline manifest; freezing never submits a provider request."""
    if not run_id.strip():
        raise ValueError("run_id must be nonempty")
    dataset = load_dataset(dataset_path, backend)
    raw_config = json.loads(config_path.read_text("utf-8"))
    config = RuntimeSnapshot.model_validate(raw_config).model_dump()
    if set(config["profiles"]) != {"generation", "judge", "reference"}:
        raise ValueError("Freeze generation, Judge and reference profiles explicitly")
    if config["profiles"]["reference"]["model_family"] in {
        config["profiles"][role]["model_family"] for role in ("generation", "judge")
    }:
        raise ValueError(
            "Reference model must belong to a different family from generation and Judge"
        )
    for profile in config["profiles"].values():
        if profile["model_name"] not in config["prices"]:
            raise ValueError("Every frozen model requires an explicit price or unknown-price entry")
    frozen = {"dataset": dataset, **asset_snapshot(backend), "config": config}
    judge_policies(frozen["templates"], config["judge"]["calibration"])
    plan: list[dict[str, Any]] = []
    cases = list(dataset["sets"]["exploration"])
    rng = random.Random(seed)
    rng.shuffle(cases)
    for case in cases:
        arms = list(ARMS)
        rng.shuffle(arms)
        first_draft = canonical_hash([run_id, case["case_id"], "first-draft"])
        for arm in arms:
            path_id = canonical_hash([run_id, case["case_id"], arm])
            plan.append(
                {
                    "path_id": path_id,
                    "case_id": case["case_id"],
                    "set_name": "exploration",
                    "arm": arm,
                    "first_draft_id": first_draft,
                    "state": "planned",
                }
            )
    protocol = {
        "seed": seed,
        "dataset_schema_hash": canonical_hash(
            json.loads(
                (backend / "evaluation" / "real_quality_cases.schema.json").read_text("utf-8")
            )
        ),
        "rag": False,
        "fewshot": False,
        "fallback": False,
        "judge_revise": False,
        "structural_max_attempts": 3,
        "sdk_max_retries": 0,
        "transport_max_additional_attempts": 2,
        "unknown_acceptance": "unresolved_no_replay",
        "reference_max_format_repairs": 2,
        "confirmation_rounds": 3,
        "confirmation_policy": "freeze one evidence-supported factor before scheduling",
    }
    identities = {name: canonical_hash(value) for name, value in frozen.items()}
    identities["execution_plan"] = canonical_hash({"protocol": protocol, "paths": plan})
    return {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "frozen": frozen,
        "protocol": protocol,
        "identities": identities,
        "execution_plan": plan,
        "provider_calls": [],
        "artifacts": [],
    }


def resume_manifest(
    *, manifest_path: Path, dataset_path: Path, config_path: Path, backend: Path
) -> dict[str, Any]:
    """Validate the frozen identity and preserve uncertain/in-flight work without replay."""
    manifest: dict[str, Any] = json.loads(manifest_path.read_text("utf-8"))
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("Unsupported real-quality manifest version")
    expected = freeze_protocol(
        dataset_path=dataset_path,
        config_path=config_path,
        backend=backend,
        run_id=manifest["run_id"],
        seed=manifest["protocol"]["seed"],
    )
    if expected["identities"] != manifest.get("identities"):
        raise ValueError("Frozen identity drift: create a new run instead of resuming")
    for name, value in manifest["frozen"].items():
        if canonical_hash(value) != manifest["identities"].get(name):
            raise ValueError(f"Frozen manifest content drift: {name}")
    plan = copy.deepcopy(manifest["execution_plan"])
    for path in plan:
        if path.get("state") not in {
            "planned",
            "in_flight",
            "completed",
            "failed",
            "unresolved",
            "cancelled",
        }:
            raise ValueError("Unknown execution path state")
        path["state"] = "planned"
    identity = canonical_hash({"protocol": manifest["protocol"], "paths": plan})
    if identity != manifest["identities"]["execution_plan"]:
        raise ValueError("Frozen execution plan drift")
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any], *, overwrite: bool = False) -> None:
    """Publish a complete local artifact atomically; new runs never overwrite existing evidence."""
    if path.exists() and not overwrite:
        raise ValueError("Manifest already exists; use resume or a new output path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)
