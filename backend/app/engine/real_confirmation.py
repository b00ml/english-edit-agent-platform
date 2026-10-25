"""Immutable, evidence-selected confirmation over the existing evaluation services."""

from __future__ import annotations

import copy
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.evaluation_protocol import (
    RuntimeSnapshot,
    canonical_hash,
    judge_policies,
    write_manifest,
)
from app.engine.real_experiment import _account_calls, _source_ids, build_evidence_report

#: Concurrent paths per confirmation run. Shared requests (first drafts, deduplicated
#: reviews) coalesce in the ledger, so this bounds provider load, not correctness.
CONFIRMATION_WORKERS = 6


def _identity(bundle: dict[str, Any]) -> str:
    design = {
        key: bundle[key]
        for key in (
            "version",
            "run_id",
            "seed",
            "selection",
            "source",
            "execution_plan",
        )
    }
    for key in ("variants", "run_definitions", "observations"):
        design[key] = bundle.get(key, {})
    return canonical_hash(design)


def amend_reference_instrument(
    bundle: dict[str, Any], *, profile: dict[str, Any], price: dict[str, Any]
) -> dict[str, Any]:
    """Swap the reference instrument of a partly executed run and re-freeze identities.

    The treatment, hypothesis, analysis and recorded outcomes stay untouched; only the
    measurement instrument changes for the not-yet-executed remainder. The operator
    must archive the pre-amendment manifest first: the previous identity is recorded
    here, and reference-based metrics become per-instrument segments.
    """
    if profile == bundle["source"]["frozen"]["config"]["profiles"]["reference"]:
        raise ValueError("reference_model must change the frozen instrument")
    previous_identity = bundle["identity"]
    for holder in (
        *bundle["variants"].values(),
        *(run["frozen"] for run in bundle["runs"].values()),
    ):
        holder["config"]["profiles"]["reference"] = copy.deepcopy(profile)
        holder["config"]["prices"][profile["model_name"]] = copy.deepcopy(price)
        _validate_runtime(holder)
    for run in bundle["runs"].values():
        run["identities"] = {name: canonical_hash(value) for name, value in run["frozen"].items()}
        run["identities"]["execution_plan"] = canonical_hash(
            {"protocol": run["protocol"], "paths": run["execution_plan"]}
        )
    bundle["run_definitions"] = {
        key: canonical_hash(
            {
                "run_id": run["run_id"],
                "protocol": run["protocol"],
                "identities": run["identities"],
                "execution_plan": [{**row, "state": "planned"} for row in run["execution_plan"]],
            }
        )
        for key, run in bundle["runs"].items()
    }
    bundle["identity"] = _identity(bundle)
    bundle.setdefault("instrument_amendments", []).append(
        {
            "previous_identity": previous_identity,
            "from_model": bundle["source"]["frozen"]["config"]["profiles"]["reference"][
                "model_name"
            ],
            "to_model": profile["model_name"],
            "amended_at": datetime.now(UTC).isoformat(),
        }
    )
    return bundle


def freeze_confirmation(
    source: dict[str, Any],
    selection: dict[str, Any],
    *,
    run_id: str,
    seed: int,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze a justified decision before any confirmation outcomes exist."""
    _validate_source(source)
    if source["protocol"]["set_name"] != "exploration":
        raise ValueError("Selection must originate from frozen exploration evidence")
    if not run_id.strip() or run_id == source["run_id"]:
        raise ValueError("Confirmation requires a new nonempty run identifier")
    bundle: dict[str, Any] = {
        "version": "real-quality-confirmation-v1",
        "run_id": run_id,
        "seed": seed,
        "selection": copy.deepcopy(selection),
        "source": copy.deepcopy(source),
        "execution_plan": [],
        "runs": {},
        "provider_calls": [],
        "observations": copy.deepcopy(observations or []),
    }
    for observation in bundle["observations"]:
        _validate_source(observation)
        if observation["frozen"] != source["frozen"]:
            raise ValueError(
                "Inherited observations must match the frozen source assets/config/dataset"
            )
    if selection.get("decision") == "no_change":
        if set(selection) != {"decision", "reason"} or not selection["reason"].strip():
            raise ValueError("A no_change decision requires a reason")
    else:
        _schedule(bundle)
    bundle["run_definitions"] = {
        key: canonical_hash(
            {name: run[name] for name in ("run_id", "protocol", "identities", "execution_plan")}
        )
        for key, run in bundle["runs"].items()
    }
    bundle["identity"] = _identity(bundle)
    return bundle


def _validate_source(source: dict[str, Any]) -> None:
    """Check the existing frozen artifact, without loading current files or profiles."""
    if source.get("version") != "real-quality-manifest-v1":
        raise ValueError("Selection requires a frozen real-quality source")
    for name, value in source["frozen"].items():
        if canonical_hash(value) != source["identities"].get(name):
            raise ValueError(f"Frozen source identity drift: {name}")
    paths = copy.deepcopy(source["execution_plan"])
    for row in paths:
        if row.get("state") not in {
            "planned",
            "in_flight",
            "completed",
            "failed",
            "unresolved",
            "cancelled",
        }:
            raise ValueError("Unknown source execution path state")
        row["state"] = "planned"
    if (
        canonical_hash({"protocol": source["protocol"], "paths": paths})
        != source["identities"]["execution_plan"]
    ):
        raise ValueError("Frozen source execution identity drift")


def _variant(source: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    required = {"decision", "factor", "key", "new", "hypothesis", "analysis", "evidence_refs"}
    allowed = required | {
        "reference_timeout_seconds",
        "reference_model",
        "generation_max_output_tokens",
        "rounds",
    }
    if (
        selection.get("decision") != "change"
        or not required <= set(selection)
        or not set(selection) <= allowed
    ):
        raise ValueError("Confirmation requires exactly one single factor")
    if not all(
        isinstance(selection[key], str) and selection[key].strip()
        for key in ("hypothesis", "analysis")
    ):
        raise ValueError("Freeze a hypothesis and analysis before confirmation")
    override = selection.get("reference_timeout_seconds")
    if override is not None and (
        isinstance(override, bool)
        or not isinstance(override, int)
        or override <= 0
        or override == source["frozen"]["config"]["reference"]["timeout_seconds"]
    ):
        raise ValueError(
            "reference_timeout_seconds must be a positive integer that changes the frozen value"
        )
    reference_override = selection.get("reference_model")
    if reference_override is not None and (
        not isinstance(reference_override, dict)
        or set(reference_override) != {"profile", "price"}
        or not isinstance(reference_override.get("profile"), dict)
        or set(reference_override["profile"])
        != {
            "name",
            "provider_id",
            "provider_config_hash",
            "model_name",
            "model_family",
            "profile_hash",
        }
        or reference_override["profile"] == source["frozen"]["config"]["profiles"]["reference"]
    ):
        raise ValueError(
            "reference_model requires a complete changed profile snapshot and an explicit price"
        )
    gen_cap = selection.get("generation_max_output_tokens")
    generation_settings = source["frozen"]["config"]["generation"]
    if gen_cap is not None and (
        isinstance(gen_cap, bool)
        or not isinstance(gen_cap, int)
        or gen_cap <= 0
        or gen_cap >= generation_settings["context_limit_tokens"]
        or gen_cap == generation_settings["max_output_tokens"]
    ):
        raise ValueError(
            "generation_max_output_tokens must change and stay below the context limit"
        )
    rounds = selection.get("rounds")
    if rounds is not None and (
        isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds < 3
    ):
        raise ValueError("rounds must reduce the pre-registered three-round schedule (1 or 2)")
    evidence = {row["path"]["path_id"] for row in build_evidence_report(source)["bad_cases"]}
    if not selection["evidence_refs"] or not set(selection["evidence_refs"]) <= evidence:
        raise ValueError("The factor must cite existing exploration evidence")
    # Frozen assets retain template-specific JSON at the validated manifest boundary.
    frozen: dict[str, Any] = copy.deepcopy(source["frozen"])
    factor, key, new = selection["factor"], selection["key"], selection["new"]
    if factor in {"generation_prompt", "feedback_prompt", "judge_prompt"}:
        generation = {
            str(t["gen_prompt"][part]).removeprefix("prompts/")
            for t in frozen["templates"].values()
            for part in ("system", "user")
        }
        generation |= {"schema-constraint.st", "json-enforce.st", "teaching-constraints.st"}
        permitted = {
            "generation_prompt": generation,
            "feedback_prompt": {"retry-feedback.st"},
            "judge_prompt": {"judge-system.st", "judge-user.st"},
        }
        if key not in permitted[factor] or not isinstance(new, str) or not new.strip():
            raise ValueError("Unsupported prompt intervention")
        parent = frozen["prompts"]
    elif factor in {"generation_skill", "judge_skill"}:
        permitted_skills = (
            {"skills/judge/SKILL.md"}
            if factor == "judge_skill"
            else {f"skills/{name}/SKILL.md" for name in (*frozen["templates"], "generate_question")}
        )
        if (
            key not in permitted_skills
            or key not in frozen["skills"]
            or not isinstance(new, str)
            or not new.strip()
        ):
            raise ValueError("Unsupported skill intervention")
        parent = frozen["skills"]
    elif factor == "generation_temperature" and key == "temperature":
        parent = frozen["config"]["generation"]
    elif factor in {"judge_threshold", "judge_weights"} and key in frozen["templates"]:
        policies = judge_policies(frozen["templates"], frozen["config"]["judge"]["calibration"])
        policy = policies[key]
        if factor == "judge_threshold":
            if new == policy["threshold"]:
                raise ValueError("The single factor must change")
            policy["threshold"] = new
        else:
            if (
                not isinstance(new, dict)
                or set(new) != set(policy["weights"])
                or new == policy["weights"]
            ):
                raise ValueError("Weights must change one complete declared policy")
            policy["weights"] = new
        frozen["config"]["judge"]["calibration"][key] = {
            **policy["weights"],
            "threshold": policy["threshold"],
        }
        judge_policies(frozen["templates"], frozen["config"]["judge"]["calibration"])
        return frozen
    elif factor in {"generation_model", "judge_model"} and key == factor.removesuffix("_model"):
        if not isinstance(new, dict) or set(new) != {"profile", "price"}:
            raise ValueError("A model factor requires its complete profile and explicit price")
        if frozen["config"]["profiles"][key] == new["profile"]:
            raise ValueError("The single factor must change")
        frozen["config"]["profiles"][key] = copy.deepcopy(new["profile"])
        frozen["config"]["prices"][new["profile"]["model_name"]] = copy.deepcopy(new["price"])
        _validate_runtime(frozen)
        return frozen
    else:
        raise ValueError("Unsupported single factor")
    if key not in parent or parent[key] == new:
        raise ValueError("The single factor must change")
    parent[key] = copy.deepcopy(new)
    _validate_runtime(frozen)
    return frozen


def _validate_runtime(frozen: dict[str, Any]) -> None:
    config = RuntimeSnapshot.model_validate(frozen["config"]).model_dump()
    frozen["config"] = config
    profiles = config["profiles"]
    if profiles["reference"]["model_family"] in {
        profiles["generation"]["model_family"],
        profiles["judge"]["model_family"],
    }:
        raise ValueError("Reference must use an independent model family")
    if any(profile["model_name"] not in config["prices"] for profile in profiles.values()):
        raise ValueError("Every model requires an explicit price snapshot")


def _schedule(bundle: dict[str, Any]) -> None:
    source, selection = bundle["source"], bundle["selection"]
    new = _variant(source, selection)
    factor = selection["factor"]
    bundle["variants"] = {"old": copy.deepcopy(source["frozen"]), "new": new}
    if factor == "feedback_prompt":
        bundle["variants"]["blind_control"] = copy.deepcopy(source["frozen"])
    override = selection.get("reference_timeout_seconds")
    if override is not None:
        # A runtime-envelope override applies uniformly to every variant; the single
        # treatment factor still differs only where _variant changed it.
        for variant in bundle["variants"].values():
            variant["config"]["reference"]["timeout_seconds"] = override
            _validate_runtime(variant)
    gen_cap = selection.get("generation_max_output_tokens")
    if gen_cap is not None:
        # Generation output headroom is an envelope parameter applied uniformly;
        # exceeding the source cap dominated validation failures by truncation.
        for variant in bundle["variants"].values():
            variant["config"]["generation"]["max_output_tokens"] = gen_cap
            _validate_runtime(variant)
    reference_override = selection.get("reference_model")
    if reference_override is not None:
        # The reference is the measurement instrument, not the treatment: swapping
        # it applies uniformly to every variant, with family isolation re-validated.
        for variant in bundle["variants"].values():
            variant["config"]["profiles"]["reference"] = copy.deepcopy(
                reference_override["profile"]
            )
            variant["config"]["prices"][reference_override["profile"]["model_name"]] = (
                copy.deepcopy(reference_override["price"])
            )
            _validate_runtime(variant)
    first_changes = factor.startswith("generation_")
    total_rounds = selection.get("rounds", 3)
    rng = random.Random(bundle["seed"])
    cases = [
        (set_name, case, repeat)
        for set_name in ("exploration", "confirmation")
        for case in source["frozen"]["dataset"]["sets"][set_name]
        for repeat in range(1, total_rounds + 1)
    ]
    if len(cases) != 60 * total_rounds:
        raise ValueError("Confirmation requires two frozen thirty-input sets and three rounds")
    rng.shuffle(cases)
    for set_name, case, repeat in cases:
        versions = list(bundle["variants"])
        rng.shuffle(versions)
        for version in versions:
            run_key = f"{version}:{repeat}"
            if run_key not in bundle["runs"]:
                bundle["runs"][run_key] = {
                    "version": source["version"],
                    "run_id": f'{bundle["run_id"]}:{run_key}',
                    "frozen": copy.deepcopy(bundle["variants"][version]),
                    "protocol": copy.deepcopy(source["protocol"]),
                    "identities": copy.deepcopy(source["identities"]),
                    "execution_plan": [],
                    "provider_calls": [],
                }
            run = bundle["runs"][run_key]
            first_id = canonical_hash(
                [bundle["run_id"], case["case_id"], repeat, version if first_changes else "shared"]
            )
            row = {
                "path_id": canonical_hash([bundle["run_id"], case["case_id"], repeat, version]),
                "first_draft_id": first_id,
                "case_id": case["case_id"],
                "set_name": set_name,
                "round": repeat,
                "version": version,
                "run_key": run_key,
                "arm": "blind_retry" if version == "blind_control" else "feedback_retry",
                "state": "planned",
            }
            bundle["execution_plan"].append(copy.deepcopy(row))
            run["execution_plan"].append(row)
    # All nested stage manifests refer to one physical ledger, including on resume.
    count = len(bundle["execution_plan"])
    semantics = {
        "generate": (
            60 * total_rounds * (1 + 2 * len(bundle["variants"]))
            if factor == "feedback_prompt"
            else count * 3
        ),
        "qc": count
        * source["frozen"]["config"]["judge"]["sample_rounds"]
        * source["frozen"]["config"]["judge"]["max_attempts"],
        "reference_solve": count * 3,
        "reference_review": count * 3,
    }
    if factor.startswith("judge_"):
        semantics["generate"] = 0
    if factor in {"judge_threshold", "judge_weights"}:
        semantics = dict.fromkeys(semantics, 0)
    for run in bundle["runs"].values():
        run["protocol"]["call_limits"] = {
            stage: {"semantic": limit, "physical": limit * 3} for stage, limit in semantics.items()
        }
        run["provider_calls"] = bundle["provider_calls"]
    if factor in {"judge_prompt", "judge_skill", "judge_model"}:
        # Acquire each missing baseline content once; Judge variants reuse this same payload.
        for repeat in range(1, 4):
            baseline = copy.deepcopy(bundle["runs"][f"old:{repeat}"])
            baseline["run_id"] = f'{bundle["run_id"]}:content:{repeat}'
            for row in baseline["execution_plan"]:
                row["path_id"] = canonical_hash(
                    [bundle["run_id"], "content", row["case_id"], repeat]
                )
                row["run_key"] = f"content:{repeat}"
            baseline["protocol"]["call_limits"]["generate"] = {"semantic": 540, "physical": 1620}
            bundle["runs"][f"content:{repeat}"] = baseline
    for run in bundle["runs"].values():
        run["identities"] = {name: canonical_hash(value) for name, value in run["frozen"].items()}
        run["identities"]["execution_plan"] = canonical_hash(
            {"protocol": run["protocol"], "paths": run["execution_plan"]}
        )


def validate_confirmation(bundle: dict[str, Any]) -> None:
    """Reject mutation of the evidence, hypothesis, treatment or planned analysis."""
    if bundle.get("version") != "real-quality-confirmation-v1" or bundle.get(
        "identity"
    ) != _identity(bundle):
        raise ValueError("Confirmation decision and execution plan are immutable; create a new run")
    expected: dict[str, list[dict[str, Any]]] = {}
    for row in bundle["execution_plan"]:
        expected.setdefault(row["run_key"], []).append(row)
    main_keys = {key for key in bundle["runs"] if not key.startswith("content:")}
    if main_keys != set(expected) or set(bundle["runs"]) != set(bundle.get("run_definitions", {})):
        raise ValueError("Confirmation runs are immutable")
    for key, run in bundle["runs"].items():
        if any(
            row.get("state")
            not in {"planned", "in_flight", "completed", "failed", "unresolved", "cancelled"}
            for row in run["execution_plan"]
        ):
            raise ValueError("Unknown confirmation execution path state")
        normalized = [{**row, "state": "planned"} for row in run["execution_plan"]]
        design = {name: run[name] for name in ("run_id", "protocol", "identities")}
        design["execution_plan"] = normalized
        variant = "old" if key.startswith("content:") else key.split(":")[0]
        if (
            canonical_hash(design) != bundle["run_definitions"][key]
            or (key in expected and normalized != expected[key])
            or run["frozen"] != bundle["variants"][variant]
        ):
            raise ValueError("Confirmation executable definitions are immutable")


def _observation(
    bundle: dict[str, Any], row: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    for source in (bundle["source"], *bundle.get("observations", [])):
        prior = next(
            (
                path
                for path in source["execution_plan"]
                if path["case_id"] == row["case_id"]
                and path["arm"] == "feedback_retry"
                and path.get("round", 1) == row["round"]
                and path["state"] == "completed"
            ),
            None,
        )
        if prior is not None:
            return source, prior
    return {}, {}


def _inherit(bundle: dict[str, Any], run: dict[str, Any], row: dict[str, Any]) -> None:
    """Reuse only the matching input and recorded round, never fabricate repeated samples."""
    source, prior = _observation(bundle, row)
    if not prior:
        return
    for stage in ("generation", "reference"):
        value = source.get(f"{stage}_results", {}).get(prior["path_id"])
        if value is not None:
            run.setdefault(f"{stage}_results", {}).setdefault(
                row["path_id"],
                {
                    **copy.deepcopy(value),
                    "inherited_from": {
                        "run_id": source["run_id"],
                        "path_id": prior["path_id"],
                        "source_hash": canonical_hash(source),
                    },
                },
            )
    if run.get("generation_results", {}).get(row["path_id"], {}).get("payload"):
        row["state"] = "completed"


def run_confirmation(
    path: Path,
    bundle: dict[str, Any],
    *,
    clients: Any,
    smoke: dict[str, Any] | None = None,
    trace_sink: Callable[..., bool | None] | None = None,
    path_limit: int | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Resume seeded paired paths through the existing durable stages and one ledger."""
    from app.engine.quality import _aggregate_score
    from app.engine.real_evaluation import _LEDGER_LOCK, run_generation
    from app.engine.real_experiment import require_reviewed_smoke
    from app.engine.real_judge import run_judge
    from app.engine.real_reference import run_reference_review

    validate_confirmation(bundle)
    if path_limit is not None and path_limit < 1:
        raise ValueError("path_limit must be positive")
    factor = bundle["selection"].get("factor", "no_change")
    offline = factor in {"no_change", "judge_threshold", "judge_weights"}
    if not offline:
        require_reviewed_smoke(bundle["source"], smoke)
    for run in bundle["runs"].values():
        run["provider_calls"] = bundle["provider_calls"]

    def persist() -> None:
        with _LEDGER_LOCK:
            # Serialize through a stripped view instead of mutating shared runs:
            # concurrent paths keep reading their run dicts (never a missing-key
            # window), and the physical ledger is written exactly once.
            stripped = {
                key: {k: v for k, v in run.items() if k != "provider_calls"}
                for key, run in bundle["runs"].items()
            }
            write_manifest(path, {**bundle, "runs": stripped}, overwrite=True)

    persist()

    def process(planned: dict[str, Any]) -> None:
        run = bundle["runs"][planned["run_key"]]
        row = next(item for item in run["execution_plan"] if item["path_id"] == planned["path_id"])
        if row["state"] == "cancelled":
            return
        if factor.startswith("judge_"):
            _inherit(bundle, run, row)
        if offline:
            observed, old = _observation(bundle, row)
            raw = observed.get("judge_results", {}).get(old.get("path_id"), {})
            case = next(
                case
                for case in run["frozen"]["dataset"]["sets"][row["set_name"]]
                if case["case_id"] == row["case_id"]
            )
            policy = judge_policies(
                run["frozen"]["templates"], run["frozen"]["config"]["judge"]["calibration"]
            )[case["template_id"]]
            if (
                row["state"] == "completed"
                and raw.get("state") == "completed"
                and raw.get("dimension_scores")
            ):
                score = round(
                    _aggregate_score(
                        run["frozen"]["templates"][case["template_id"]]["quality_rules"],
                        raw["dimension_scores"],
                        policy["weights"],
                    ),
                    2,
                )
                run.setdefault("judge_results", {})[row["path_id"]] = {
                    **copy.deepcopy(raw),
                    "score": score,
                    "passed": score >= policy["threshold"],
                    "threshold": policy["threshold"],
                    "weights": policy["weights"],
                    "inherited_from": {
                        "run_id": observed["run_id"],
                        "path_id": old["path_id"],
                        "source_hash": canonical_hash(observed),
                    },
                }
            return
        bound = clients(run) if callable(clients) else clients[planned["version"]]
        kwargs: dict[str, Any] = {"path_ids": {row["path_id"]}, "persist": persist}
        if trace_sink is not None:
            kwargs["trace_sink"] = trace_sink
        if factor.startswith("judge_") and row["state"] != "completed":
            acquisition = bundle["runs"][f'content:{row["round"]}']
            content_path = next(
                item for item in acquisition["execution_plan"] if item["case_id"] == row["case_id"]
            )
            baseline = clients(acquisition) if callable(clients) else clients["old"]
            content_kwargs = {**kwargs, "path_ids": {content_path["path_id"]}}
            run_generation(path, acquisition, client=baseline["generation"], **content_kwargs)
            run_reference_review(path, acquisition, client=baseline["reference"], **content_kwargs)
            row["state"] = content_path["state"]
            for stage in ("generation", "reference"):
                result = acquisition.get(f"{stage}_results", {}).get(content_path["path_id"])
                if result is not None:
                    run.setdefault(f"{stage}_results", {})[row["path_id"]] = copy.deepcopy(result)
        if not factor.startswith("judge_"):
            run_generation(path, run, client=bound["generation"], **kwargs)
        run_judge(path, run, client=bound["judge"], **kwargs)
        run_reference_review(path, run, client=bound["reference"], **kwargs)
        persist()

    # A bounded execution prefix retains the full frozen denominator and order on disk.
    prefix = bundle["execution_plan"][:path_limit]
    if offline:
        for planned in prefix:
            process(planned)
    else:
        # Bounded concurrency over independent paths: shared requests (first drafts,
        # content-addressed reviews) coalesce inside the ledger, so parallel versions
        # never issue duplicate physical sends. Tests asserting exact send counts
        # pass workers=1 for deterministic serial accounting.
        max_workers = workers if workers is not None else CONFIRMATION_WORKERS
        if max_workers <= 1:
            for planned in prefix:
                process(planned)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(process, prefix))
    persist()
    return bundle


def confirmation_report(bundle: dict[str, Any]) -> dict[str, Any]:
    """Report planned denominators and actual physical billing without sending calls."""
    validate_confirmation(bundle)
    flat = copy.deepcopy(bundle["source"])
    flat["execution_plan"] = []
    flat["provider_calls"] = bundle["provider_calls"]
    for stage in ("generation", "judge", "reference"):
        flat[f"{stage}_results"] = {}
    for run in bundle["runs"].values():
        if ":content:" in run["run_id"]:
            continue
        flat["execution_plan"].extend(run["execution_plan"])
        for stage in ("generation", "judge", "reference"):
            flat[f"{stage}_results"].update(run.get(f"{stage}_results", {}))

    def group(rows: list[dict[str, Any]]) -> dict[str, Any]:
        requested = set().union(
            *(
                _source_ids(flat, row["path_id"], stage)
                for row in rows
                for stage in ("generation", "judge", "reference")
            )
        )
        calls = [
            call
            for call in bundle["provider_calls"]
            if call["call_id"] in requested or call.get("request_id") in requested
        ]
        return build_evidence_report({**flat, "execution_plan": rows, "provider_calls": calls})

    main = [row for row in flat["execution_plan"] if row["version"] != "blind_control"]

    def contrast(rows: list[dict[str, Any]]) -> dict[str, Any]:
        pairs: dict[tuple[str, int], dict[str, str]] = {}
        for row in rows:
            pairs.setdefault((row["case_id"], row["round"]), {})[row["version"]] = row["path_id"]
        result = {}
        for stage, label in (("judge", "passed"), ("reference", "reference_pass")):
            by_input: dict[str, list[int]] = {}
            for (case_id, _), pair in pairs.items():
                outcomes = flat[f"{stage}_results"]
                old, new = (outcomes.get(pair.get(version), {}) for version in ("old", "new"))
                if (
                    old.get("state") == new.get("state") == "completed"
                    and type(old.get(label)) is bool
                    and type(new.get(label)) is bool
                ):
                    by_input.setdefault(case_id, []).append(int(new[label]) - int(old[label]))
            means = {case: sum(values) / len(values) for case, values in by_input.items()}
            paired = sum(len(values) for values in by_input.values())
            result[stage] = {
                "planned_pairs": len(pairs),
                "paired_observations": paired,
                "missing_pairs": len(pairs) - paired,
                "paired_inputs": len(means),
                "input_differences": means,
                "change_percentage_points": (
                    100 * sum(means.values()) / len(means) if means else None
                ),
            }
        return result

    contrasts = contrast(main)
    paired_cases = set().union(*(set(value["input_differences"]) for value in contrasts.values()))
    template_by_case = {
        case["case_id"]: case["template_id"]
        for cases in bundle["source"]["frozen"]["dataset"]["sets"].values()
        for case in cases
    }
    groups = {
        f"{version}:{set_name}": group(
            [
                row
                for row in flat["execution_plan"]
                if row["version"] == version and row["set_name"] == set_name
            ]
        )
        for version in bundle.get("variants", {})
        for set_name in ("exploration", "confirmation")
    }
    return {
        "decision": bundle["selection"],
        "decision_identity": bundle["identity"],
        "planned_main_paths": len(main),
        "planned_control_paths": len(flat["execution_plan"]) - len(main),
        "unique_inputs": len({row["case_id"] for row in main}),
        "paired_inputs": len(paired_cases),
        "groups": groups,
        "contrasts": contrasts,
        "contrasts_by_set": {
            name: contrast([row for row in main if row["set_name"] == name])
            for name in ("exploration", "confirmation")
        },
        "contrasts_by_template": {
            name: contrast([row for row in main if template_by_case[row["case_id"]] == name])
            for name in bundle["source"]["frozen"]["templates"]
        },
        "contrasts_by_round": {
            str(repeat): contrast([row for row in main if row["round"] == repeat])
            for repeat in range(1, 4)
        },
        "evidence": group(flat["execution_plan"]),
        "physical": _account_calls(bundle["provider_calls"]),
        "limitations": [
            "Rounds are repeated observations of sixty inputs, not independent samples.",
            "Judge policy changes measure gate behavior, not generated-content quality.",
            "Absent raw scores or content remain missing; no replacement samples are invented.",
        ],
    }
