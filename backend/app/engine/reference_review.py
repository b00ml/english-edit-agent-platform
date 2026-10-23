"""Offline reference-model review packets and Judge comparison metrics."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.prompt_loader import load_prompt

REFERENCE_RUBRIC_VERSION = "english-content-review-v1"


class RubricDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["pass", "fail", "unresolved"]
    evidence: str = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def require_evidence_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("每个评审维度都必须提供证据")
        return value


class ReferenceRubric(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer_correctness: RubricDimension
    explanation_correctness: RubricDimension
    ambiguity: RubricDimension
    knowledge_point_fit: RubricDimension
    difficulty_fit: RubricDimension


class ReferenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    review_id: str = Field(min_length=1)
    status: Literal["reviewed", "unresolved"]
    reference_model: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    rubric: ReferenceRubric
    solved_answer: str | list[str] | None = None
    solve_reason: str | None = None
    reference_pass: bool | None
    judge_pass: bool | None = None
    judge_model: str = Field(min_length=1)
    judge_rubric_version: str = Field(min_length=1)
    disagreement_reason: str | None = None
    unresolved_reason: str | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> ReferenceAssessment:
        verdicts = [
            self.rubric.answer_correctness.verdict,
            self.rubric.explanation_correctness.verdict,
            self.rubric.ambiguity.verdict,
            self.rubric.knowledge_point_fit.verdict,
            self.rubric.difficulty_fit.verdict,
        ]
        if "fail" in verdicts:
            derived_status: str = "reviewed"
            derived_pass: bool | None = False
        elif "unresolved" in verdicts:
            derived_status = "unresolved"
            derived_pass = None
        else:
            derived_status = "reviewed"
            derived_pass = True
        if self.status != derived_status or self.reference_pass is not derived_pass:
            raise ValueError(
                "overall status/reference_pass must follow the itemized rubric verdicts"
            )
        if self.status == "reviewed":
            if self.judge_pass is None:
                raise ValueError("已确认参考评审必须包含非空 judge_pass 标签")
            if self.reference_pass is None or self.solved_answer is None or not self.solve_reason:
                raise ValueError("已完成参考评审必须包含独立答案、理由和通过判定")
            if self.unresolved_reason is not None:
                raise ValueError("已完成参考评审不能包含待裁决原因")
            if self.reference_pass != self.judge_pass and not self.disagreement_reason:
                raise ValueError("模型判断分歧必须记录原因")
        else:
            if self.reference_pass is not None or not self.unresolved_reason:
                raise ValueError("待裁决评审必须保留原因且不能有参考通过/驳回判定")
            if self.disagreement_reason is not None:
                raise ValueError("待裁决评审不能伪装成已确认的模型分歧")
        return self


def review_packet_id(case_id: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"case_id": case_id, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def build_review_packets(arms: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    """Return deduplicated content-only packets, with arm/config/Judge data withheld."""
    candidates: dict[str, dict[str, object]] = {}
    instructions = load_prompt("reference-review-instructions.st")
    for arm in arms.values():
        outcomes = arm.get("outcomes", [])
        if not isinstance(outcomes, list):
            continue
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            payload = outcome.get("payload")
            case_id = outcome.get("case_id")
            if (
                not outcome.get("final_schema_success")
                or not isinstance(payload, dict)
                or not isinstance(case_id, str)
            ):
                continue
            review_id = review_packet_id(case_id, payload)
            candidates[review_id] = {
                "review_id": review_id,
                "content": payload,
                "instructions": instructions,
                "rubric_version": REFERENCE_RUBRIC_VERSION,
            }
    return [candidates[key] for key in sorted(candidates)]


def summarize_reference_reviews(
    records: list[dict[str, object]],
) -> dict[str, object]:
    assessments = [ReferenceAssessment.model_validate(item) for item in records]
    if len({item.review_id for item in assessments}) != len(assessments):
        raise ValueError("参考评审 review_id 必须唯一")

    judge_records = [item for item in assessments if item.judge_pass is not None]
    judge_passes = sum(item.judge_pass is True for item in judge_records)
    reviewed = [item for item in assessments if item.status == "reviewed"]
    agreement_count = sum(item.reference_pass == item.judge_pass for item in reviewed)
    disagreement_cases = [
        item.review_id for item in reviewed if item.reference_pass != item.judge_pass
    ]
    unresolved_cases = [item.review_id for item in assessments if item.status == "unresolved"]

    kappa: float | None = None
    kappa_status = "unavailable_no_confirmed_comparisons"
    if reviewed:
        reference_yes = sum(item.reference_pass is True for item in reviewed)
        judge_yes = sum(item.judge_pass is True for item in reviewed)
        n = len(reviewed)
        observed = agreement_count / n
        expected = (reference_yes * judge_yes + (n - reference_yes) * (n - judge_yes)) / (n * n)
        if expected >= 1:
            kappa_status = "unavailable_degenerate_marginals"
        else:
            kappa = round((observed - expected) / (1 - expected), 4)
            kappa_status = "available"

    return {
        "source": "model_reference_review",
        "sample_count": len(assessments),
        "confirmed_reference_pass_rate": {
            "numerator": sum(item.reference_pass is True for item in reviewed),
            "denominator": len(reviewed),
            "rate": (
                round(sum(item.reference_pass is True for item in reviewed) / len(reviewed), 4)
                if reviewed
                else None
            ),
        },
        "judge_pass_rate": {
            "numerator": judge_passes,
            "denominator": len(judge_records),
            "rate": round(judge_passes / len(judge_records), 4) if judge_records else None,
        },
        "model_judge_comparison": {
            "denominator": len(reviewed),
            "agreement_count": agreement_count,
            "agreement_rate": round(agreement_count / len(reviewed), 4) if reviewed else None,
            "cohen_kappa": kappa,
            "cohen_kappa_status": kappa_status,
        },
        "disagreement_cases": disagreement_cases,
        "unresolved_cases": unresolved_cases,
        "versions": {
            "reference_models": sorted({item.reference_model for item in assessments}),
            "reference_rubrics": sorted({item.rubric_version for item in assessments}),
            "judge_models": sorted({item.judge_model for item in assessments}),
            "judge_rubrics": sorted({item.judge_rubric_version for item in assessments}),
        },
        "assessments": [item.model_dump(mode="json") for item in assessments],
        "limitations": [
            "This is model-to-model agreement, not human accuracy "
            "or proof of teaching correctness.",
            "Unresolved reviews are excluded from confirmed reference "
            "pass/fail and agreement metrics.",
        ],
    }
