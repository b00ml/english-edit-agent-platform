"""Versioned, human-attested gold datasets; no LLM labels are manufactured here."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.engine.agreement import cohen_kappa
from app.versioning import hash_value


class HumanAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    human_attested: Literal[True]
    payload_hash: str = Field(min_length=64, max_length=64)
    annotated_at: datetime
    answer_correct: bool
    explanation_correct: bool
    unambiguous: bool
    knowledge_match: bool
    difficulty_match: bool
    expected_answer: str | list[str]
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

    @field_validator("human_attested", mode="before")
    @classmethod
    def require_human_attestation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("必须显式声明 human_attested 为布尔 true")
        return True

    @model_validator(mode="after")
    def require_annotation_timezone(self) -> HumanAnnotation:
        if self.annotated_at.tzinfo is None:
            raise ValueError("人工标注时间必须包含时区")
        return self

    def labels(self) -> tuple[bool, ...]:
        return (
            self.answer_correct,
            self.explanation_correct,
            self.unambiguous,
            self.knowledge_match,
            self.difficulty_match,
        )


class GoldCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    status: Literal["candidate", "reviewed"] = "candidate"
    template_id: str
    params: dict[str, object]
    payload: dict[str, object]
    payload_hash: str
    versions: dict[str, object]
    auto_score: float = Field(ge=0, le=100, allow_inf_nan=False)
    threshold: float = Field(ge=0, le=100, allow_inf_nan=False)
    annotations: list[HumanAnnotation] = Field(default_factory=list)
    adjudication: HumanAnnotation | None = None
    rag_provenance: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provenance(self) -> GoldCase:
        if hash_value(self.payload) != self.payload_hash:
            raise ValueError("payload hash 不匹配，题目已被修改")
        for annotation in self.annotations + ([self.adjudication] if self.adjudication else []):
            if annotation.payload_hash != self.payload_hash:
                raise ValueError("标注针对的内容版本不匹配")
        if self.adjudication is not None and self.adjudication.reviewer in {
            a.reviewer for a in self.annotations
        }:
            raise ValueError("仲裁者必须独立于原标注者")
        if self.status == "reviewed":
            if len(self.annotations) != 2 or len({a.reviewer for a in self.annotations}) != 2:
                raise ValueError("金标必须由两名不同人工标注者复核")
            required = (
                "template_hash",
                "prompt_hash",
                "skill_hash",
                "model_name",
                "judge_model",
                "judge_prompt_hash",
                "judge_skill_hash",
            )
            if self.versions.get("threshold_verified") is not True:
                raise ValueError("金标必须有实际生效阈值的快照证据")
            if any(not self.versions.get(key) for key in required):
                raise ValueError("金标缺少模板/Prompt/Skill/模型/Judge 版本")
            if (self.annotations[0].labels(), self.annotations[0].expected_answer) != (
                self.annotations[1].labels(),
                self.annotations[1].expected_answer,
            ):
                if self.adjudication is None or self.adjudication.reviewer in {
                    a.reviewer for a in self.annotations
                }:
                    raise ValueError("争议题必须由第三名人工仲裁")
        return self

    def truth(self) -> HumanAnnotation:
        if self.status != "reviewed":
            raise ValueError("候选题集未经人工标注，不得作为金标评测")
        return self.adjudication or self.annotations[0]


def gold_report(cases: list[GoldCase]) -> dict[str, object]:
    """每题按自身阈值二值化，同时保留正确性/唯一性等逐项人类真值。"""
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("数据集不能为空，case_id 不得重复")
    if len({case.dataset_version for case in cases}) != 1:
        raise ValueError("不能混合不同 dataset_version")
    truth = [case.truth() for case in cases]
    accepted = [all(label.labels()) for label in truth]
    predicted = [case.auto_score >= case.threshold for case in cases]

    def metrics(indices: list[int]) -> dict[str, float | int | None]:
        n = len(indices)
        auto_pass = [i for i in indices if predicted[i]]
        tp = sum(accepted[i] and predicted[i] for i in indices)
        tn = sum(not accepted[i] and not predicted[i] for i in indices)
        fp = sum(not accepted[i] and predicted[i] for i in indices)
        fn = sum(accepted[i] and not predicted[i] for i in indices)
        return {
            "n": n,
            "auto_passed": len(auto_pass),
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "accuracy": (tp + tn) / n,
            "false_pass_rate": fp / len(auto_pass) if auto_pass else None,
            "human_rejection_rate": sum(not accepted[i] for i in indices) / n,
            "answer_error_rate": (
                sum(not truth[i].answer_correct for i in auto_pass) / len(auto_pass)
                if auto_pass
                else None
            ),
            "explanation_error_rate": (
                sum(not truth[i].explanation_correct for i in auto_pass) / len(auto_pass)
                if auto_pass
                else None
            ),
            "ambiguity_rate": (
                sum(not truth[i].unambiguous for i in auto_pass) / len(auto_pass)
                if auto_pass
                else None
            ),
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "kappa": (
                cohen_kappa([accepted[i] for i in indices], [predicted[i] for i in indices])
                if len({accepted[i] for i in indices} | {predicted[i] for i in indices}) > 1
                else None
            ),
        }

    strata: dict[str, list[int]] = defaultdict(list)
    rag_groups: dict[str, list[int]] = defaultdict(list)
    for i, case in enumerate(cases):
        rag_groups[str(case.rag_provenance.get("status", "unknown"))].append(i)
        strata[f"{case.template_id}/{case.params.get('difficulty', 'unknown')}"].append(i)
    return {
        "dataset_version": cases[0].dataset_version,
        "dataset_hash": hash_value([c.model_dump(mode="json") for c in cases]),
        "generated_at": datetime.now().astimezone().isoformat(),
        "small_sample_warning": len(cases) < 30,
        "overall": metrics(list(range(len(cases)))),
        "by_stratum": {key: metrics(indices) for key, indices in strata.items()},
        "by_rag_status": {key: metrics(indices) for key, indices in rag_groups.items()},
        "versions": {case.case_id: case.versions for case in cases},
        "scope": "仅描述已标注样本；非随机生产样本不得据此宣称整体质量达标",
    }
