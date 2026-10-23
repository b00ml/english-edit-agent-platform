import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.engine.reference_review import (
    ReferenceAssessment,
    build_review_packets,
    review_packet_id,
    summarize_reference_reviews,
)


def _rubric(fail_dimension=None, unresolved=False):
    dimensions = (
        "answer_correctness",
        "explanation_correctness",
        "ambiguity",
        "knowledge_point_fit",
        "difficulty_fit",
    )
    return {
        dimension: {
            "verdict": (
                "unresolved" if unresolved else "fail" if dimension == fail_dimension else "pass"
            ),
            "evidence": f"Evidence for {dimension}.",
        }
        for dimension in dimensions
    }


def _review_versions(record):
    return {
        "rubric": _rubric(
            fail_dimension=(
                "ambiguity"
                if record["status"] == "reviewed" and record["reference_pass"] is False
                else None
            ),
            unresolved=record["status"] == "unresolved",
        ),
        "solved_answer": "walked" if record["status"] == "reviewed" else None,
        "solve_reason": "past-time marker" if record["status"] == "reviewed" else None,
        "judge_model": "judge-v1",
        "judge_rubric_version": "judge-rubric-v1",
    }


def test_reference_report_excludes_unresolved_cases_from_comparison():
    records = [
        {
            "review_id": "a",
            "status": "reviewed",
            "reference_pass": True,
            "judge_pass": True,
            "reference_model": "reviewer-v1",
            "rubric_version": "rubric-v1",
        },
        {
            "review_id": "b",
            "status": "reviewed",
            "reference_pass": False,
            "judge_pass": True,
            "reference_model": "reviewer-v1",
            "rubric_version": "rubric-v1",
            "disagreement_reason": "two options appear valid",
        },
        {
            "review_id": "c",
            "status": "unresolved",
            "reference_pass": None,
            "judge_pass": False,
            "reference_model": "reviewer-v1",
            "rubric_version": "rubric-v1",
            "unresolved_reason": "insufficient source evidence",
        },
    ]
    records = [{**record, **_review_versions(record)} for record in records]

    report = summarize_reference_reviews(records)

    assert report["source"] == "model_reference_review"
    assert report["sample_count"] == 3
    assert report["judge_pass_rate"] == {"numerator": 2, "denominator": 3, "rate": 0.6667}
    assert report["model_judge_comparison"]["denominator"] == 2
    assert report["model_judge_comparison"]["agreement_count"] == 1
    assert report["model_judge_comparison"]["agreement_rate"] == 0.5
    assert report["model_judge_comparison"]["cohen_kappa"] == 0.0
    assert report["disagreement_cases"] == ["b"]
    assert report["unresolved_cases"] == ["c"]


def test_degenerate_model_judge_marginals_make_kappa_unavailable():
    records = [
        {
            "review_id": "a",
            "status": "reviewed",
            "reference_pass": True,
            "judge_pass": True,
            "reference_model": "reviewer-v1",
            "rubric_version": "rubric-v1",
        },
        {
            "review_id": "b",
            "status": "reviewed",
            "reference_pass": True,
            "judge_pass": True,
            "reference_model": "reviewer-v1",
            "rubric_version": "rubric-v1",
        },
    ]
    records = [{**record, **_review_versions(record)} for record in records]

    report = summarize_reference_reviews(records)

    assert report["model_judge_comparison"]["agreement_rate"] == 1.0
    assert report["model_judge_comparison"]["cohen_kappa"] is None
    assert (
        report["model_judge_comparison"]["cohen_kappa_status"] == "unavailable_degenerate_marginals"
    )


def test_reviewed_assessment_cannot_treat_missing_judge_label_as_disagreement():
    record = {
        "review_id": "missing-judge-label",
        "status": "reviewed",
        "reference_model": "reviewer-v1",
        "rubric_version": "rubric-v1",
        "rubric": _rubric(),
        "solved_answer": "walked",
        "solve_reason": "Past-time marker.",
        "reference_pass": True,
        "judge_pass": None,
        "judge_model": "judge-v1",
        "judge_rubric_version": "judge-rubric-v1",
        "disagreement_reason": "Judge label missing from source data.",
    }

    with pytest.raises(ValidationError, match="judge_pass"):
        summarize_reference_reviews([record])


def test_unresolved_assessment_may_omit_judge_label():
    record = {
        "review_id": "unresolved-without-judge-label",
        "status": "unresolved",
        "reference_model": "reviewer-v1",
        "rubric_version": "rubric-v1",
        "rubric": _rubric(unresolved=True),
        "reference_pass": None,
        "judge_model": "judge-v1",
        "judge_rubric_version": "judge-rubric-v1",
        "unresolved_reason": "Insufficient source evidence.",
    }

    report = summarize_reference_reviews([record])

    assert report["judge_pass_rate"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert report["model_judge_comparison"]["denominator"] == 0
    assert report["unresolved_cases"] == ["unresolved-without-judge-label"]


def test_cli_imports_reference_assessments_and_keeps_blind_packet_separate(tmp_path):
    backend = Path(__file__).parents[1]
    source = tmp_path / "evaluation.json"
    assessments = tmp_path / "assessments.jsonl"
    output = tmp_path / "reviewed-evaluation.json"
    source.write_text(
        json.dumps(
            {
                "case_count": 1,
                "cases_by_template": {"single_choice": 1},
                "provenance": {"code_revision": "test-revision", "dataset_hash": "test-hash"},
                "reference_review_packets": [
                    {
                        "review_id": review_packet_id(
                            "single_choice-01", {"stem": "She ___ yesterday.", "answer": "walked"}
                        ),
                        "content": {"stem": "She ___ yesterday.", "answer": "walked"},
                        "instructions": "First solve independently, then assess content.",
                        "rubric_version": "english-content-review-v1",
                    }
                ],
                "arms": {
                    "single_attempt": {
                        "counts": {
                            "final_schema_success": 1,
                            "recovered": 0,
                            "recovery_denominator": 0,
                            "failed": 0,
                            "missing": 0,
                            "cancelled": 0,
                            "unresolved": 0,
                        },
                        "denominators": {"planned_cases": 1},
                        "attempts": {"adapter_calls": 1},
                        "outcomes": [
                            {
                                "case_id": "single_choice-01",
                                "final_schema_success": True,
                                "payload": {"stem": "She ___ yesterday.", "answer": "walked"},
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assessments.write_text(
        json.dumps(
            {
                "review_id": review_packet_id(
                    "single_choice-01", {"stem": "She ___ yesterday.", "answer": "walked"}
                ),
                "status": "reviewed",
                "reference_model": "external-model-v1",
                "rubric_version": "english-content-review-v1",
                "rubric": _rubric(),
                "solved_answer": "walked",
                "solve_reason": "The time marker yesterday requires past tense.",
                "reference_pass": True,
                "judge_pass": True,
                "judge_model": "production-judge-v1",
                "judge_rubric_version": "judge-rubric-v1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(backend / "scripts" / "structured_retry_eval.py"),
            "review",
            "--evaluation",
            str(source),
            "--assessments",
            str(assessments),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["reference_review"]["source"] == "model_reference_review"
    assert report["reference_review"]["sample_count"] == 1
    packet = report["reference_review_packets"][0]
    assert packet["content"] == {"stem": "She ___ yesterday.", "answer": "walked"}
    assert "arm" not in packet
    assert "judge" not in json.dumps(packet).lower()
    assert "generation" not in json.dumps(packet).lower()
    assert "human_accuracy" not in report["reference_review"]
    assert report["reference_review"]["by_arm"]["single_attempt"]["sample_count"] == 1
    assert "by_arm" not in report["reference_review_packets"][0]


def test_review_packets_hide_arm_and_generation_details_and_deduplicate_same_content():
    payload = {"stem": "She ___ yesterday.", "answer": "walked"}
    arms = {
        "single_attempt": {
            "outcomes": [
                {"case_id": "single_choice-01", "final_schema_success": True, "payload": payload}
            ]
        },
        "feedback_retry": {
            "outcomes": [
                {"case_id": "single_choice-01", "final_schema_success": True, "payload": payload}
            ]
        },
    }

    packets = build_review_packets(arms)

    assert len(packets) == 1
    assert packets[0]["content"] == payload
    assert "arm" not in json.dumps(packets[0]).lower()
    assert "judge" not in json.dumps(packets[0]).lower()
    assert "generation" not in json.dumps(packets[0]).lower()


def test_itemized_rubric_requires_every_dimension_with_evidence():
    record = {
        "review_id": "rubric-test",
        "status": "reviewed",
        "reference_model": "reviewer-v1",
        "rubric_version": "rubric-v1",
        "rubric": _rubric(),
        "solved_answer": "walked",
        "solve_reason": "Past-time marker.",
        "reference_pass": True,
        "judge_pass": True,
        "judge_model": "judge-v1",
        "judge_rubric_version": "judge-rubric-v1",
    }

    ReferenceAssessment.model_validate(record)

    missing_dimension = {
        **record,
        "rubric": {key: value for key, value in _rubric().items() if key != "difficulty_fit"},
    }
    with pytest.raises(ValidationError):
        ReferenceAssessment.model_validate(missing_dimension)

    missing_evidence = {
        **record,
        "rubric": {**_rubric(), "answer_correctness": {"verdict": "pass", "evidence": " "}},
    }
    with pytest.raises(ValidationError):
        ReferenceAssessment.model_validate(missing_evidence)

    unsupported_overall = {
        **record,
        "rubric": _rubric(fail_dimension="answer_correctness"),
    }
    with pytest.raises(ValidationError, match="overall status/reference_pass"):
        ReferenceAssessment.model_validate(unsupported_overall)
