"""Offline few-shot isolation, comparison, and model-review provenance helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy.orm import Session

from app.models import ContentItem, GenerationTask, SamplePool
from app.versioning import hash_value

RUBRIC_DIMENSIONS = (
    "answer_correctness",
    "explanation_correctness",
    "ambiguity",
    "knowledge_point_fit",
    "difficulty_fit",
)
_VERDICTS = {"pass", "fail", "unresolved"}


def create_model_reviewed_sample(
    session: Session,
    item: ContentItem,
    assessment: dict[str, Any],
    *,
    source_family: str,
) -> SamplePool:
    """Persist a model assessment as its own source, never as a human quality record."""
    reviewer_model = assessment.get("reviewer_model")
    rubric_version = assessment.get("rubric_version")
    judgments = assessment.get("judgments")
    if not isinstance(reviewer_model, str) or not reviewer_model.strip():
        raise ValueError("reviewer_model is required")
    if not isinstance(rubric_version, str) or not rubric_version.strip():
        raise ValueError("rubric_version is required")
    if not isinstance(judgments, dict) or set(judgments) != set(RUBRIC_DIMENSIONS):
        raise ValueError("all five rubric dimensions are required")
    for dimension, judgment in judgments.items():
        if not isinstance(judgment, dict) or judgment.get("verdict") not in _VERDICTS:
            raise ValueError(f"invalid verdict for {dimension}")
        if not isinstance(judgment.get("evidence"), str) or not judgment["evidence"].strip():
            raise ValueError(f"evidence is required for {dimension}")
    if not isinstance(source_family, str) or not source_family.strip():
        raise ValueError("source_family is required")
    if session.query(SamplePool).filter_by(item_id=item.id).first():
        raise ValueError("item already has a sample pool record")

    task = session.get(GenerationTask, item.task_id)
    if task is None:
        raise ValueError("source generation task is missing")
    verdicts = {judgment["verdict"] for judgment in judgments.values()}
    status = (
        "rejected" if "fail" in verdicts else "pending" if "unresolved" in verdicts else "passed"
    )
    payload_hash = hash_value(item.payload)
    sample = SamplePool(
        item_id=item.id,
        template_id=item.template_id,
        source="model_review",
        purpose="fewshot",
        knowledge_point=(task.params or {}).get("knowledge_point"),
        payload=item.payload,
        tenant_id=item.tenant_id,
        meta={
            "source_family": source_family.strip(),
            "review": {
                "source": "model",
                "status": status,
                "reviewer_model": reviewer_model.strip(),
                "rubric_version": rubric_version.strip(),
                "judgments": judgments,
                "payload_hash": payload_hash,
            },
        },
    )
    session.add(sample)
    session.flush()
    return sample


def model_review_candidate(sample: SamplePool) -> dict[str, Any]:
    """Create the evaluator input for a persisted model-reviewed sample."""
    return {
        "candidate_id": sample.id,
        "item_id": sample.item_id,
        "source_family": (sample.meta or {}).get("source_family"),
        "payload": sample.payload,
        "review": (sample.meta or {}).get("review"),
    }


def build_fewshot_evaluation(
    *,
    candidates: list[dict[str, Any]],
    evaluation_cases: list[dict[str, Any]],
    outcomes: dict[str, dict[str, bool]],
    config: dict[str, Any],
    near_duplicate_threshold: float = 0.85,
) -> dict[str, Any]:
    """Build a frozen, offline report after excluding item/family/text leakage."""
    if not 0 < near_duplicate_threshold <= 1:
        raise ValueError("near_duplicate_threshold must be in (0, 1]")
    case_ids = [case.get("case_id") for case in evaluation_cases]
    candidate_ids = [candidate.get("candidate_id") for candidate in candidates]
    if any(not value for value in [*case_ids, *candidate_ids]):
        raise ValueError("case_id and candidate_id are required")
    if len(set(case_ids)) != len(case_ids) or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("case_id and candidate_id values must be unique")

    eligible: list[str] = []
    excluded = {
        "pending_or_failed_review": 0,
        "same_item": 0,
        "same_source_family": 0,
        "near_duplicate": 0,
    }
    for candidate in candidates:
        reason = ""
        if not _model_review_passed(candidate.get("review")):
            excluded["pending_or_failed_review"] += 1
            continue
        candidate_text = _normalized_payload(candidate.get("payload"))
        for case in evaluation_cases:
            if candidate.get("item_id") and candidate.get("item_id") == case.get("item_id"):
                reason = "same_item"
                break
            family = candidate.get("source_family")
            if family and family == case.get("source_family"):
                reason = "same_source_family"
                break
            case_text = _normalized_payload(case.get("payload"))
            if (
                candidate_text
                and case_text
                and SequenceMatcher(None, candidate_text, case_text).ratio()
                >= near_duplicate_threshold
            ):
                reason = "near_duplicate"
                break
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(candidate["candidate_id"])

    paired = [
        case_id
        for case_id in case_ids
        if case_id in outcomes
        and isinstance(outcomes[case_id].get("without_fewshot"), bool)
        and isinstance(outcomes[case_id].get("with_fewshot"), bool)
    ]
    without_count = sum(bool(outcomes[case_id]["without_fewshot"]) for case_id in paired)
    with_count = sum(bool(outcomes[case_id]["with_fewshot"]) for case_id in paired)
    denominator = len(paired)
    without_rate = without_count / denominator if denominator else None
    with_rate = with_count / denominator if denominator else None
    report = {
        "version": 1,
        "status": "pending_review",
        "config": config,
        "config_hash": hash_value(config),
        "dataset": {
            "planned_case_count": len(case_ids),
            "case_ids": case_ids,
            "candidate_count": len(candidates),
        },
        "candidate_split": {
            "eligible_candidate_ids": eligible,
            "excluded": excluded,
            "near_duplicate_threshold": near_duplicate_threshold,
        },
        "candidate_payload_hashes": {
            candidate["candidate_id"]: hash_value(candidate["payload"])
            for candidate in candidates
            if candidate["candidate_id"] in eligible
        },
        "effect": {
            "paired_denominator": denominator,
            "unresolved_or_missing_count": len(case_ids) - denominator,
            "without_fewshot": {
                "numerator": without_count,
                "denominator": denominator,
                "rate": without_rate,
            },
            "with_fewshot": {
                "numerator": with_count,
                "denominator": denominator,
                "rate": with_rate,
            },
            "delta": round(with_rate - without_rate, 6) if denominator else None,
        },
    }
    report["report_hash"] = _report_hash(report)
    return report


def review_fewshot_evaluation(
    session: Session,
    report: dict[str, Any],
    *,
    reviewer: str,
    approve: bool = False,
) -> dict[str, Any]:
    """Record the human operational decision against the exact frozen report and payloads."""
    report_hash = report.get("report_hash")
    if not isinstance(report_hash, str) or report_hash != _report_hash(report):
        raise ValueError("evaluation report hash is missing or invalid")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer is required")
    if approve and not report.get("effect", {}).get("paired_denominator"):
        raise ValueError("cannot approve few-shot evaluation without paired outcomes")
    candidate_hashes = report.get("candidate_payload_hashes") or {}
    now = datetime.now(UTC).isoformat()
    reviewed_samples = []
    for sample_id in report["candidate_split"]["eligible_candidate_ids"]:
        sample = session.get(SamplePool, sample_id)
        if sample is None or sample.source != "model_review":
            raise ValueError(f"model-reviewed sample {sample_id} is missing")
        meta = dict(sample.meta or {})
        review = dict(meta.get("review") or {})
        if review.get("source") != "model" or review.get("status") != "passed":
            raise ValueError(
                f"model-reviewed sample {sample_id} has not passed all rubric dimensions"
            )
        payload_hash = hash_value(sample.payload)
        if (
            review.get("payload_hash") != payload_hash
            or candidate_hashes.get(sample_id) != payload_hash
        ):
            raise ValueError(f"model-reviewed sample {sample_id} payload changed after evaluation")
        reviewed_samples.append((sample, payload_hash))
    for sample, payload_hash in reviewed_samples:
        meta = dict(sample.meta or {})
        meta["evaluation_approval"] = {
            "report_hash": report_hash,
            "reviewer": reviewer.strip(),
            "reviewed_at": now,
            "approved": bool(approve),
            "payload_hash": payload_hash,
        }
        sample.meta = meta
    report["review"] = {
        "reviewer": reviewer.strip(),
        "reviewed_at": now,
        "approved": bool(approve),
    }
    return report


def _normalized_payload(payload: Any) -> str:
    def strings(value: Any):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key in sorted(value):
                yield from strings(value[key])
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    normalized = "".join(
        character if character.isalnum() else " "
        for character in " ".join(strings(payload)).casefold()
    )
    return re.sub(r"\s+", " ", normalized).strip()


def _model_review_passed(review: Any) -> bool:
    if (
        not isinstance(review, dict)
        or review.get("source") != "model"
        or review.get("status") != "passed"
    ):
        return False
    judgments = review.get("judgments")
    return bool(
        review.get("reviewer_model")
        and review.get("rubric_version")
        and isinstance(judgments, dict)
        and set(judgments) == set(RUBRIC_DIMENSIONS)
        and all(
            isinstance(judgments.get(dimension), dict)
            and judgments[dimension].get("verdict") == "pass"
            and judgments[dimension].get("evidence")
            for dimension in RUBRIC_DIMENSIONS
        )
    )


def _report_hash(report: dict[str, Any]) -> str:
    content = {key: value for key, value in report.items() if key not in {"report_hash", "review"}}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
