"""Compose frozen evaluation stages and report their durable local evidence."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.evaluation_protocol import canonical_hash, write_manifest
from app.engine.real_evaluation import run_generation
from app.engine.real_judge import run_judge
from app.engine.real_reference import run_reference_review
from app.engine.trace import record_trace


def _smoke_evidence(smoke: dict[str, Any]) -> str:
    plan = smoke["execution_plan"]
    calls = smoke.get("provider_calls", [])
    if (
        smoke["protocol"].get("set_name") != "smoke"
        or len(plan) != 9
        or not all(row["state"] == "completed" for row in plan)
        or not all(
            smoke.get(stage, {}).get(row["path_id"], {}).get("state") == "completed"
            for stage in ("judge_results", "reference_results")
            for row in plan
        )
        or not {"generate", "qc", "reference_solve", "reference_review"}.issubset(
            {row["stage"] for row in calls}
        )
        or not all(row.get("trace_state") == "completed" for row in calls)
    ):
        raise ValueError("Smoke execution and Trace persistence must be complete before review")
    return canonical_hash(
        {
            "identities": smoke["identities"],
            "plan": plan,
            "calls": calls,
            "generation": smoke.get("generation_results"),
            "judge": smoke["judge_results"],
            "reference": smoke["reference_results"],
        }
    )


def record_smoke_review(smoke: dict[str, Any]) -> None:
    """Record an explicit operator acknowledgement only after inspecting actual smoke evidence."""
    smoke["operator_review"] = {
        "evidence_hash": _smoke_evidence(smoke),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "scope": "contexts, blind inputs, Trace, usage and local persistence inspected",
    }


def require_reviewed_smoke(manifest: dict[str, Any], smoke: dict[str, Any] | None) -> None:
    """Block formal sends until separately executed smoke evidence has been acknowledged."""
    if manifest["protocol"].get("set_name") == "smoke":
        return
    if smoke is None or smoke.get("operator_review", {}).get("evidence_hash") != _smoke_evidence(
        smoke
    ):
        raise ValueError("Exploration requires reviewed smoke evidence")
    exploration_assets = {k: v for k, v in manifest["identities"].items() if k != "execution_plan"}
    smoke_assets = {k: v for k, v in smoke["identities"].items() if k != "execution_plan"}
    if exploration_assets != smoke_assets:
        raise ValueError("Reviewed smoke does not match frozen exploration assets/config/dataset")


def run_experiment(
    path: Path,
    manifest: dict[str, Any],
    *,
    clients: dict[str, Any],
    smoke: dict[str, Any] | None = None,
    trace_sink: Callable[..., bool | None] = record_trace,
) -> dict[str, Any]:
    """Resume generation, production Judge and independent reference through existing services."""
    require_reviewed_smoke(manifest, smoke)
    for role, executor in (
        ("generation", run_generation),
        ("judge", run_judge),
        ("reference", run_reference_review),
    ):
        executor(path, manifest, client=clients[role], trace_sink=trace_sink)
    write_manifest(path, manifest, overwrite=True)
    return manifest


def _source_ids(manifest: dict[str, Any], path_id: str, stage: str) -> set[str]:
    row = manifest.get(f"{stage}_results", {}).get(path_id, {})
    if stage == "generation":
        return {attempt["source_call_id"] for attempt in row.get("attempts", [])}
    return set(row.get("source_call_ids", []))


def _counts(manifest: dict[str, Any], paths: list[dict[str, Any]], stage: str) -> dict[str, int]:
    counts = {
        key: 0
        for key in (
            "planned",
            "submitted",
            "completed",
            "success",
            "failure",
            "cancelled",
            "missing",
            "unresolved",
            "pending_review",
        )
    }
    counts["planned"] = len(paths)
    sent_ids = {
        identity
        for call in manifest.get("provider_calls", [])
        for identity in (call["call_id"], call.get("request_id"))
    }
    for path in paths:
        row = manifest.get(f"{stage}_results", {}).get(path["path_id"], {})
        state = path["state"] if stage == "generation" else row.get("state", "missing")
        counts["submitted"] += bool(_source_ids(manifest, path["path_id"], stage) & sent_ids)
        counts["completed"] += state in {"completed", "failed"}
        verdict = (
            True
            if stage == "generation"
            else row.get("passed" if stage == "judge" else "reference_pass")
        )
        counts["success"] += state == "completed" and verdict is True
        counts["failure"] += state == "failed" or (state == "completed" and verdict is False)
        counts["cancelled"] += state == "cancelled"
        counts["missing"] += state in {"missing", "planned"}
        counts["unresolved"] += state in {"unresolved", "in_flight"}
        counts["pending_review"] += (
            stage == "reference" and bool(row.get("questions")) and verdict is None
        )
    return counts


def _account_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    known = [row for row in calls if row.get("cost") is not None]
    currencies = sorted({row["currency"] for row in calls if row.get("currency")})
    complete = (
        len(known) == len(calls)
        and len(currencies) <= 1
        and all(row.get("trace_state") == "completed" for row in calls)
    )
    latencies = sorted(row["latency_ms"] for row in calls if row.get("latency_ms") is not None)
    return {
        "call_count": len(calls),
        "semantic_request_count": len({row.get("request_id", row["call_id"]) for row in calls}),
        "cost": {
            "complete": complete,
            "total": sum(row["cost"] for row in known) if complete else None,
            "known_subtotal": sum(row["cost"] for row in known) if len(currencies) <= 1 else None,
            "known_by_currency": {
                currency: sum(row["cost"] for row in known if row.get("currency") == currency)
                for currency in currencies
            },
            "known_calls": len(known),
            "denominator": len(calls),
            "currencies": currencies,
        },
        "usage_coverage": {
            "reported": sum(row.get("usage") is not None for row in calls),
            "denominator": len(calls),
        },
        "trace_coverage": {
            "persisted": sum(row.get("trace_state") == "completed" for row in calls),
            "denominator": len(calls),
        },
        "provider_latency_ms": {
            "observed_calls": len(latencies),
            "sum": sum(latencies),
            "p50": latencies[math.ceil(len(latencies) * 0.5) - 1] if latencies else None,
            "p95": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        },
    }


def build_evidence_report(manifest: dict[str, Any]) -> dict[str, Any]:
    """Report all planned paths, including missing/unknown outcomes; never submit a call."""
    paths = manifest["execution_plan"]
    calls = manifest.get("provider_calls", [])
    stages = ("generation", "judge", "reference")
    case_lookup = {
        row["case_id"]: row
        for rows in manifest["frozen"]["dataset"]["sets"].values()
        for row in rows
    }

    def group(selected: list[dict[str, Any]]) -> dict[str, Any]:
        logical = []
        for path in selected:
            requested = set().union(
                *(_source_ids(manifest, path["path_id"], stage) for stage in stages)
            )
            logical.extend(
                row
                for row in calls
                if row["call_id"] in requested or row.get("request_id") in requested
            )
        return {
            "stages": {stage: _counts(manifest, selected, stage) for stage in stages},
            "logical": _account_calls(logical),
        }

    bad_cases, disagreements = [], []
    pairs: dict[str, tuple[bool, bool]] = {}
    paired_paths = 0
    for path in paths:
        judge = manifest.get("judge_results", {}).get(path["path_id"], {})
        reference = manifest.get("reference_results", {}).get(path["path_id"], {})
        decisive = judge.get("state") == reference.get("state") == "completed"
        disagreement = (
            decisive
            and type(judge.get("passed")) is bool
            and type(reference.get("reference_pass")) is bool
            and judge["passed"] != reference["reference_pass"]
        )
        if (
            decisive
            and type(judge.get("passed")) is bool
            and type(reference.get("reference_pass")) is bool
        ):
            pairs.setdefault(
                reference.get("content_id", path["path_id"]),
                (judge["passed"], reference["reference_pass"]),
            )
            paired_paths += 1
        if disagreement:
            disagreements.append(
                {
                    "path_id": path["path_id"],
                    "case_id": path["case_id"],
                    "content_id": reference.get("content_id"),
                    "judge_pass": judge["passed"],
                    "reference_pass": reference["reference_pass"],
                }
            )
        if (
            any(
                state in {"failed", "unresolved", "in_flight"}
                for state in (path["state"], judge.get("state"), reference.get("state"))
            )
            or judge.get("passed") is False
            or reference.get("reference_pass") is False
            or (reference.get("questions") and reference.get("reference_pass") is None)
        ):
            bad_cases.append(
                {
                    "path": path,
                    "generation": manifest.get("generation_results", {}).get(path["path_id"]),
                    "judge": judge,
                    "reference": reference,
                }
            )
    expected = None
    if pairs:
        judge_yes = sum(judge for judge, _ in pairs.values()) / len(pairs)
        reference_yes = sum(reference for _, reference in pairs.values()) / len(pairs)
        expected = judge_yes * reference_yes + (1 - judge_yes) * (1 - reference_yes)
    agreement = (
        sum(judge == reference for judge, reference in pairs.values()) / len(pairs)
        if pairs
        else None
    )
    return {
        "run_id": manifest["run_id"],
        "identities": manifest["identities"],
        "set_name": manifest["protocol"].get("set_name"),
        "planned_paths": len(paths),
        "input_cases": len({p["case_id"] for p in paths}),
        "stages": {stage: _counts(manifest, paths, stage) for stage in stages},
        "physical": _account_calls(calls),
        "physical_by_stage": {
            stage: _account_calls([row for row in calls if row["stage"] == stage])
            for stage in sorted({row["stage"] for row in calls})
        },
        "arms": {
            arm: group([p for p in paths if p["arm"] == arm])
            for arm in sorted({p["arm"] for p in paths})
        },
        "templates": {
            name: group([p for p in paths if case_lookup[p["case_id"]]["template_id"] == name])
            for name in sorted({case_lookup[p["case_id"]]["template_id"] for p in paths})
        },
        "unknown_cost_call_ids": [row["call_id"] for row in calls if row.get("cost") is None],
        "bad_cases": bad_cases,
        "disagreements": disagreements,
        "model_agreement": {
            "paired": len(pairs),
            "paired_paths": paired_paths,
            "planned": len(paths),
            "agreement": agreement,
            "kappa": (
                (agreement - expected) / (1 - expected)
                if expected is not None and expected != 1
                else None
            ),
        },
        "provenance": manifest["frozen"]["dataset"].get("provenance", "unspecified"),
        "limitations": [
            "Judge/reference agreement measures two model judgements, not human gold accuracy.",
            "Logical arm estimates allocate reused physical calls independently; they are not extra billed calls.",
            "Provider latency is per-send time, not task end-to-end latency or a throughput benchmark.",
            "Exploration selects hypotheses; improvements require a separately frozen confirmation run.",
        ],
    }


def interview_brief(report: dict[str, Any]) -> str:
    """Keep local interview wording tied to measured counts and explicit attribution."""
    return (
        f"# 真实质量探索证据（本地）\n\n运行：{report['run_id']}；输入 {report['input_cases']}；计划路径 {report['planned_paths']}。\n\n"
        f"结构有效 {report['stages']['generation']['success']}；Judge 通过 {report['stages']['judge']['success']}；参考通过 {report['stages']['reference']['success']}，分母均为全部计划路径。\n\n"
        f"实际物理调用 {report['physical']['call_count']}；已知费用小计 {report['physical']['cost']['known_subtotal']}；完整费用 {report['physical']['cost']['total']}。未知费用不按零处理。\n\n"
        "参考评审来自模型，不能宣称人工金标准或已证明质量提升。用户负责方案决策与人工核验，AI 辅助编码，执行器自动运行，Judge 与参考模型提供各自判断。\n"
    )
