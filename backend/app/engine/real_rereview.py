"""Freeze disagreement sampling and reuse blind review without overwriting initial evidence."""

from __future__ import annotations

import copy
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.engine.evaluation_protocol import canonical_hash, write_manifest
from app.engine.real_confirmation import (
    _validate_source,
    confirmation_report,
    validate_confirmation,
)
from app.engine.real_experiment import _account_calls, _counts, build_evidence_report
from app.engine.real_reference import run_reference_review
from app.engine.trace import record_trace

# Frozen manifests and provider factories are the existing dynamic artifact/schema boundaries.


def _manifests(original: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if original.get("version") == "real-quality-confirmation-v1":
        validate_confirmation(original)
        return {
            "source": original["source"],
            **{f"observation:{i}": row for i, row in enumerate(original.get("observations", []))},
            **original["runs"],
        }
    _validate_source(original)
    return {"source": original}


def _pool(original: dict[str, Any]) -> list[dict[str, Any]]:
    contents: dict[str, dict[str, Any]] = {}
    for run_key, manifest in _manifests(original).items():
        for path in manifest["execution_plan"]:
            payload = manifest.get("generation_results", {}).get(path["path_id"], {}).get("payload")
            if path["state"] != "completed" or not isinstance(payload, dict):
                continue
            case = next(
                case
                for case in manifest["frozen"]["dataset"]["sets"][path["set_name"]]
                if case["case_id"] == path["case_id"]
            )
            context = {
                key: case.get(key)
                for key in ("template_id", "params", "teaching_goal", "scope", "judgment_basis")
            }
            identity = canonical_hash({"context": context, "payload": payload})
            entry = contents.setdefault(
                identity,
                {
                    "content_id": identity,
                    "content_hash": canonical_hash(payload),
                    "context_hash": canonical_hash(context),
                    "memberships": [],
                    "disagreement": False,
                },
            )
            judge = manifest.get("judge_results", {}).get(path["path_id"], {})
            reference = manifest.get("reference_results", {}).get(path["path_id"], {})
            entry["disagreement"] |= (
                judge.get("state") == reference.get("state") == "completed"
                and type(judge.get("passed")) is bool
                and type(reference.get("reference_pass")) is bool
                and judge["passed"] != reference["reference_pass"]
            )
            entry["memberships"].append({"run_key": run_key, "path": copy.deepcopy(path)})
    return [contents[key] for key in sorted(contents)]


def _definition(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run[key]
        for key in (
            "run_id",
            "frozen",
            "protocol",
            "execution_plan",
            "generation_results",
            "reference_teaching_context",
        )
    }


def _identity(bundle: dict[str, Any]) -> str:
    return canonical_hash(
        {
            key: bundle[key]
            for key in (
                "version",
                "run_id",
                "seed",
                "original",
                "pool",
                "pool_hash",
                "selection",
                "review_definitions",
            )
        }
    )


def freeze_rereview(original: dict[str, Any], *, run_id: str, seed: int) -> dict[str, Any]:
    """Union all decisive disagreements with up to six random context/content pairs."""
    manifests = _manifests(original)
    if not run_id.strip() or run_id in {row["run_id"] for row in manifests.values()} | {
        original["run_id"]
    }:
        raise ValueError("Rereview requires a fresh nonempty run identifier")
    pool = _pool(original)
    disagreements = [row for row in pool if row["disagreement"]]
    random_rows = random.Random(seed).sample(pool, min(6, len(pool)))
    selected_by_id: dict[str, dict[str, Any]] = {}
    for reason, rows in (("disagreement", disagreements), ("random", random_rows)):
        for row in rows:
            entry = selected_by_id.setdefault(
                row["content_id"], {**copy.deepcopy(row), "reasons": []}
            )
            entry["reasons"].append(reason)
    selected = list(selected_by_id.values())
    reviews = {}
    for entry in selected:
        member = entry["memberships"][0]
        source = manifests[member["run_key"]]
        path = member["path"]
        case = next(
            case
            for case in source["frozen"]["dataset"]["sets"][path["set_name"]]
            if case["case_id"] == path["case_id"]
        )
        run = {
            "run_id": f"{run_id}:{entry['content_id']}",
            "frozen": copy.deepcopy(source["frozen"]),
            "protocol": copy.deepcopy(source["protocol"]),
            "execution_plan": [copy.deepcopy(path)],
            "generation_results": {
                path["path_id"]: {
                    "payload": copy.deepcopy(
                        source["generation_results"][path["path_id"]]["payload"]
                    )
                }
            },
            "reference_teaching_context": {
                key: case[key]
                for key in ("teaching_goal", "scope", "judgment_basis")
                if key in case
            },
        }
        run["reference_teaching_context"].update(
            {
                key: case["params"][key]
                for key in (
                    "grade",
                    "difficulty",
                    "knowledge_point",
                    "topic",
                    "constraints",
                    "quantity",
                    "blank_count",
                    "question_count",
                )
                if key in case["params"]
            }
        )
        # Each selected pair gets one two-phase review, at most two format repairs per phase.
        for stage in ("reference_solve", "reference_review"):
            run["protocol"]["call_limits"][stage] = {
                "semantic": 3 * len(selected),
                "physical": 9 * len(selected),
            }
        reviews[entry["content_id"]] = run
    bundle = {
        "version": "real-quality-rereview-v1",
        "run_id": run_id,
        "seed": seed,
        "original": copy.deepcopy(original),
        "pool": pool,
        "pool_hash": canonical_hash(pool),
        "selection": selected,
        "reviews": reviews,
        "provider_calls": [],
        "review_definitions": {
            key: canonical_hash(_definition(run)) for key, run in reviews.items()
        },
    }
    bundle["identity"] = _identity(bundle)
    return bundle


def validate_rereview(bundle: dict[str, Any]) -> None:
    """Reject edited sampling, original assessments, teaching inputs or executable definitions."""
    if (
        bundle.get("version") != "real-quality-rereview-v1"
        or bundle.get("identity") != _identity(bundle)
        or set(bundle["reviews"]) != set(bundle["review_definitions"])
        or any(
            canonical_hash(_definition(run)) != bundle["review_definitions"][key]
            for key, run in bundle["reviews"].items()
        )
    ):
        raise ValueError("Rereview inputs and selection are immutable; create a new run")


def run_rereview(
    path: Path,
    bundle: dict[str, Any],
    *,
    clients: Callable[[dict[str, Any]], dict[str, Any]],
    trace_sink: Callable[..., bool | None] = record_trace,
) -> dict[str, Any]:
    """Resume the one fresh review per selected pair; terminal unknown requests stay terminal."""
    validate_rereview(bundle)

    def persist() -> None:
        durable = {
            **bundle,
            "reviews": {
                key: {name: value for name, value in run.items() if name != "provider_calls"}
                for key, run in bundle["reviews"].items()
            },
        }
        write_manifest(path, durable, overwrite=True)

    persist()
    for run in bundle["reviews"].values():
        path_id = run["execution_plan"][0]["path_id"]
        if run.get("reference_results", {}).get(path_id, {}).get("state") in {
            "completed",
            "failed",
            "unresolved",
            "cancelled",
        }:
            continue
        run["provider_calls"] = bundle["provider_calls"]
        run_reference_review(
            path, run, client=clients(run)["reference"], trace_sink=trace_sink, persist=persist
        )
    persist()
    return bundle


def _agreement(pairs: list[tuple[bool, bool]], planned: int) -> dict[str, Any]:
    n = len(pairs)
    observed = sum(left == right for left, right in pairs) / n if n else None
    kappa = None
    status = "unavailable_no_confirmed_comparisons"
    if n:
        left_yes = sum(left for left, _ in pairs) / n
        right_yes = sum(right for _, right in pairs) / n
        if len({left for left, _ in pairs}) < 2 or len({right for _, right in pairs}) < 2:
            status = "unavailable_degenerate_marginals"
        else:
            expected = left_yes * right_yes + (1 - left_yes) * (1 - right_yes)
            assert observed is not None
            kappa = (observed - expected) / (1 - expected)
            status = "available"
    return {
        "planned": planned,
        "paired": n,
        "unresolved_or_missing": planned - n,
        "agreement": observed,
        "kappa": kappa,
        "kappa_status": status,
    }


def _comparisons(bundle: dict[str, Any]) -> dict[str, Any]:
    manifests = _manifests(bundle["original"])
    pairs: dict[str, list[tuple[bool, bool]]] = {
        "judge_initial_reference": [],
        "judge_rereview": [],
        "initial_reference_rereview": [],
    }
    planned = 0
    for selected in bundle["selection"]:
        run = bundle["reviews"][selected["content_id"]]
        fresh = run.get("reference_results", {}).get(run["execution_plan"][0]["path_id"], {})
        for member in selected["memberships"]:
            manifest = manifests[member["run_key"]]
            path_id = member["path"]["path_id"]
            judge = manifest.get("judge_results", {}).get(path_id, {})
            initial = manifest.get("reference_results", {}).get(path_id, {})
            planned += 1
            for name, left, left_label, right, right_label in (
                ("judge_initial_reference", judge, "passed", initial, "reference_pass"),
                ("judge_rereview", judge, "passed", fresh, "reference_pass"),
                ("initial_reference_rereview", initial, "reference_pass", fresh, "reference_pass"),
            ):
                if (
                    left.get("state") == right.get("state") == "completed"
                    and type(left.get(left_label)) is bool
                    and type(right.get(right_label)) is bool
                ):
                    pairs[name].append((left[left_label], right[right_label]))
    return {name: _agreement(values, planned) for name, values in pairs.items()}


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    """Project known numeric fields; arbitrary currency/config/provider text never crosses."""
    currencies = account["cost"]["currencies"]
    safe_currency = (
        currencies[0] if len(currencies) == 1 and currencies[0] in {"CNY", "USD"} else None
    )
    return {
        "call_count": account["call_count"],
        "semantic_request_count": account["semantic_request_count"],
        "cost": {
            "currency": safe_currency,
            "complete": account["cost"]["complete"],
            "total": account["cost"]["total"] if safe_currency else None,
            "known_subtotal": account["cost"]["known_subtotal"] if safe_currency else None,
            "known_calls": account["cost"]["known_calls"],
            "denominator": account["cost"]["denominator"],
        },
        "usage_coverage": {
            key: account["usage_coverage"][key] for key in ("reported", "denominator")
        },
        "trace_coverage": {
            key: account["trace_coverage"][key] for key in ("persisted", "denominator")
        },
        "tokens": {
            field: {
                key: account["tokens"][field][key]
                for key in ("total", "known_subtotal", "reported_calls", "denominator")
            }
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
    }


def build_evidence_package(bundle: dict[str, Any]) -> dict[str, Any]:
    """Build complete local evidence plus a fixed-method/config-hash/aggregate public candidate."""
    validate_rereview(bundle)
    original = bundle["original"]
    manifests = _manifests(original)
    reports = {}
    all_calls = {}
    for key, manifest in manifests.items():
        calls = (
            original["provider_calls"]
            if key in original.get("runs", {})
            else manifest.get("provider_calls", [])
        )
        reports[key] = build_evidence_report({**manifest, "provider_calls": calls})
        all_calls.update({row["call_id"]: row for row in calls})
    all_calls.update({row["call_id"]: row for row in bundle["provider_calls"]})
    sampling = {
        "eligible_unique_contents": len(bundle["pool"]),
        "selected": len(bundle["selection"]),
        "disagreements": sum("disagreement" in row["reasons"] for row in bundle["selection"]),
        "random": sum("random" in row["reasons"] for row in bundle["selection"]),
        "overlap": sum(
            {"disagreement", "random"}.issubset(row["reasons"]) for row in bundle["selection"]
        ),
        "selected_path_memberships": sum(len(row["memberships"]) for row in bundle["selection"]),
    }
    stages = []
    for run in bundle["reviews"].values():
        stages.append(
            _counts(
                {**run, "provider_calls": bundle["provider_calls"]},
                run["execution_plan"],
                "reference",
            )
        )
    states = {
        key: sum(row[key] for row in stages)
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
    physical = _account_calls(list(all_calls.values()))
    rereview_physical = _account_calls(bundle["provider_calls"])
    comparisons = _comparisons(bundle)
    confirmation = (
        confirmation_report(original)
        if original.get("version") == "real-quality-confirmation-v1"
        else None
    )
    local = {
        "original": copy.deepcopy(original),
        "selection": copy.deepcopy(bundle["selection"]),
        "pool_hash": bundle["pool_hash"],
        "sampling": sampling,
        "initial_reports": reports,
        "confirmation": confirmation,
        "rereview_states": states,
        "rereview_records": {
            key: copy.deepcopy(run.get("reference_results", {}))
            for key, run in bundle["reviews"].items()
        },
        "rereview_calls": copy.deepcopy(bundle["provider_calls"]),
        "rereview_physical": rereview_physical,
        "all_physical": physical,
        "comparisons": comparisons,
    }
    # Construct only trusted constants, SHA identities and explicitly enumerated aggregate fields.
    public = {
        "version": "real-quality-public-candidate-v1",
        "publication": "requires_separate_approval",
        "method": "union_all_decisive_disagreements_and_seeded_up_to_six_context_unique_contents",
        "review": "one_fresh_blind_independent_solve_then_five_rubric_review",
        "config_hashes": sorted(
            {canonical_hash(run["frozen"]["config"]) for run in manifests.values()}
        ),
        "asset_hashes": sorted(
            {
                canonical_hash(
                    {
                        key: value
                        for key, value in run["frozen"].items()
                        if key not in {"config", "dataset"}
                    }
                )
                for run in manifests.values()
            }
        ),
        "sampling": dict(sampling),
        "rereview_states": dict(states),
        "comparisons": comparisons,
        "physical": _public_account(physical),
        "rereview_physical": _public_account(rereview_physical),
        "initial_stages": [
            {
                "planned_paths": report["planned_paths"],
                "input_cases": report["input_cases"],
                "stages": {
                    stage: {key: report["stages"][stage][key] for key in states}
                    for stage in ("generation", "judge", "reference")
                },
            }
            for report in reports.values()
        ],
        "limits": [
            "model_agreement_is_not_human_accuracy",
            "selected_review_is_not_population_estimate",
            "repeated_paths_are_not_independent_inputs",
            "unresolved_is_not_a_truth_label",
        ],
    }
    return {"local": local, "summary_zh": _summary(local), "public_candidate": public}


def _summary(local: dict[str, Any]) -> str:
    sampling, states, cost = (
        local["sampling"],
        local["rereview_states"],
        local["all_physical"]["cost"],
    )
    lines = [
        "# 真实质量闭环证据（仅本地）",
        "",
        f"去重候选 {sampling['eligible_unique_contents']} 个，复核 {sampling['selected']} 个："
        f"全部分歧 {sampling['disagreements']} 个，随机入选 {sampling['random']} 个，"
        f"重叠 {sampling['overlap']} 个只复核一次；"
        f"保留 {sampling['selected_path_memberships']} 个原路径成员关系。",
        "",
        "| 阶段 | 计划 | 已提交 | 已完成 | 成功 | 失败 | 取消 | 缺失 | 未决 | 待复核 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    keys = (
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
    for name, report in local["initial_reports"].items():
        for stage in ("generation", "judge", "reference"):
            lines.append(
                f"| 首评 {name}/{stage} | "
                + " | ".join(str(report["stages"][stage][key]) for key in keys)
                + " |"
            )
    lines.extend(
        [
            "| 新上下文复评 | " + " | ".join(str(states[key]) for key in keys) + " |",
            "",
            f"实际物理调用 {local['all_physical']['call_count']} 次；费用完整={cost['complete']}，"
            f"总费用={cost['total']}，已知分币种小计={cost['known_by_currency']}，"
            f"已知费用调用 {cost['known_calls']}/{cost['denominator']}。未知费用不能按零计算。",
            f"Trace 覆盖={local['all_physical']['trace_coverage']}；"
            f"usage 覆盖={local['all_physical']['usage_coverage']}；"
            f"Token 账目={local['all_physical']['tokens']}。",
            "",
            "用户决策：协议、单因素选择及 no_change 决策按原始冻结材料保留；"
            "不从运行结果推断用户完成了未记录的动作。",
            "用户验证：只认可原始材料中明确记录的 operator_review；未记录即不能声称人工验收。",
            "AI 实现：由编程代理实现协议、执行器、测试与报告；不据此归为用户手写实现。",
            "自动化执行：调用账本与 Trace 证明实际执行范围；计划数不等于已执行数。",
            "模型判断：首评与复评独立保留，未决不裁成真值；两者都不是专家标签。",
            "",
            "模型间一致性与 Kappa 只描述模型判断；任一标签边际退化时 Kappa unavailable。"
            "分歧富集样本不能外推总体质量，重复路径和三轮观测不是独立输入；不能声称人工准确率。",
            f"选中路径比较（原始成员口径）：{local['comparisons']}。",
            "",
            "公开候选仅包含固定方法、配置/资产哈希和聚合数字；当前仅生成本地文件，发布另行审批。",
        ]
    )
    if local["confirmation"] is not None:
        confirmation = local["confirmation"]
        lines.insert(
            3,
            f"冻结决策：{confirmation['decision']}；确认计划主路径 "
            f"{confirmation['planned_main_paths']}，控制路径 "
            f"{confirmation['planned_control_paths']}，独立输入 {confirmation['unique_inputs']}。"
            "零收益、负收益和 no_change 均为有效结论。",
        )
    return "\n".join(lines) + "\n"
