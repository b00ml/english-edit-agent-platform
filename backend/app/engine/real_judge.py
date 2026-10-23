"""Frozen evaluation of final schema-valid content with the production Judge."""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Collection
from pathlib import Path
from typing import Any, Callable

from app.engine.evaluation_protocol import canonical_hash, judge_policies
from app.engine.quality import run_quality_check
from app.engine.real_evaluation import EvaluationLedger, evaluation_template
from app.engine.structured_output import StructuredResponse
from app.engine.trace import record_trace
from app.errors import EvaluationTransportError, EvaluationUnresolvedError, QualityCheckError

# Manifests and provider envelopes retain dynamic template-specific schema fields at this boundary.


def _account(manifest: dict[str, Any]) -> None:
    results = manifest["judge_results"]
    calls = {row["call_id"]: row for row in manifest["provider_calls"] if row["stage"] == "qc"}

    def total(ids: Collection[str]) -> float | None:
        return (
            sum(calls[key]["cost"] for key in ids)
            if all(calls[key].get("cost") is not None for key in ids)
            else None
        )

    def rate(denominator: int) -> dict[str, Any]:
        numerator = sum(row.get("passed") is True for row in results.values())
        return {
            "numerator": numerator,
            "denominator": denominator,
            "rate": numerator / denominator if denominator else None,
        }

    arms: dict[str, Any] = {}
    for planned in manifest["execution_plan"]:
        requested = set(results.get(planned["path_id"], {}).get("source_call_ids", []))
        ids = {
            key
            for key, row in calls.items()
            if key in requested or row.get("request_id") in requested
        }
        entry = arms.setdefault(planned["arm"], {"source_call_ids": []})
        entry["source_call_ids"].extend(sorted(ids))
    for entry in arms.values():
        entry["logical_cost"] = total(entry["source_call_ids"])
    manifest["judge_accounting"] = {
        "state_counts": dict(Counter(row["state"] for row in results.values())),
        "pass_rate_all_paths": rate(len(manifest["execution_plan"])),
        "pass_rate_schema_valid": rate(
            sum(row.get("eligible") is True for row in results.values())
        ),
        "pass_rate_completed": rate(sum(row["state"] == "completed" for row in results.values())),
        "physical_call_count": len(calls),
        "physical_cost": total(set(calls)),
        "arms": arms,
        "trace_coverage": {
            "complete": all(row["trace_state"] == "completed" for row in calls.values()),
            "unresolved_call_ids": sorted(
                key for key, row in calls.items() if row["trace_state"] != "completed"
            ),
        },
    }


def run_judge(
    path: Path,
    manifest: dict[str, Any],
    *,
    client: Any,
    trace_sink: Callable[..., bool | None] = record_trace,
) -> dict[str, Any]:
    """Score each frozen final content once; preserve a result per logical path."""
    ledger = EvaluationLedger(path, manifest, trace_sink=trace_sink)
    frozen = manifest["frozen"]
    config = frozen["config"]
    judge = config["judge"]
    profile = config["profiles"]["judge"]
    model = profile["model_name"]
    policies = judge_policies(frozen["templates"], judge["calibration"])
    transport = client.with_options(max_retries=0, timeout=config["generation"]["timeout_seconds"])
    results = manifest.setdefault("judge_results", {})
    for planned in manifest["execution_plan"]:
        path_id = planned["path_id"]
        if results.get(path_id, {}).get("state") in {
            "completed",
            "failed",
            "unresolved",
            "cancelled",
        }:
            continue
        generation = manifest.get("generation_results", {}).get(path_id, {})
        if planned["state"] != "completed" or not isinstance(generation.get("payload"), dict):
            results[path_id] = {
                "state": "cancelled" if planned["state"] == "cancelled" else "missing",
                "eligible": False,
                "generation_state": planned["state"],
                "error": "No completed schema-valid final generation",
                "source_call_ids": [],
            }
            continue
        case = next(
            row
            for row in frozen["dataset"]["sets"][planned["set_name"]]
            if row["case_id"] == planned["case_id"]
        )
        payload = generation["payload"]
        template = evaluation_template(manifest, case, 1)
        calibration = judge["calibration"].get(case["template_id"], {})
        weights = policies[case["template_id"]]["weights"]
        threshold = policies[case["template_id"]]["threshold"]
        content_id = canonical_hash({"case": case, "payload": payload})
        prior = next(
            (
                row
                for row in results.values()
                if row.get("content_id") == content_id and row["state"] != "in_flight"
            ),
            None,
        )
        if prior is not None:
            results[path_id] = copy.deepcopy(prior)
            continue
        result: dict[str, Any] = {
            "content_id": content_id,
            "state": "in_flight",
            "eligible": True,
            "threshold": threshold,
            "weights": weights,
            "calibration_snapshot": calibration,
            "model_profile": copy.deepcopy(profile),
            "source_call_ids": [],
        }
        results[path_id] = result
        current_id = ""

        def adapter(
            messages: list[dict[str, str]], sample_round: int, attempt: int
        ) -> StructuredResponse:
            nonlocal current_id
            request_id = canonical_hash(
                [manifest["run_id"], "qc", content_id, judge, sample_round, attempt]
            )
            try:
                response = ledger.call(
                    request_id,
                    messages,
                    stage="qc",
                    model=model,
                    price=config["prices"][model],
                    metadata={"judge": judge},
                    invoke=lambda: transport.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=judge["temperature"],
                        response_format={"type": "json_object"},
                    ),
                )
            finally:
                result["source_call_ids"].extend(ledger.source_call_ids(request_id))
                ledger.save()
            current_id = response.source_call_id
            return response

        def recorder(**trace: Any) -> None:
            trace["input_data"].update(
                model_profile=copy.deepcopy(profile),
                weights=weights,
                threshold=threshold,
                calibration_snapshot=calibration,
            )
            ledger.record_validation(current_id, **trace)

        try:
            score, dimensions = run_quality_check(
                template,
                payload,
                model_name=model,
                trace_id=path_id,
                template_id=case["template_id"],
                rounds=judge["sample_rounds"],
                weights_override=weights,
                generation_params=case["params"],
                response_adapter=adapter,
                trace_sink=recorder,
                temperature=judge["temperature"],
                max_attempts=judge["max_attempts"],
            )
            result.update(
                state="completed",
                score=score,
                dimension_scores=dimensions,
                passed=score >= threshold,
            )
        except (QualityCheckError, EvaluationTransportError) as exc:
            result.update(state="failed", error=str(exc))
        except EvaluationUnresolvedError as exc:
            result.update(state="unresolved", error=str(exc))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            result.update(state="failed", error=str(exc), error_type=type(exc).__name__)
            ledger.save()
            raise
        _account(manifest)
        ledger.save()
    _account(manifest)
    ledger.save()
    return manifest
