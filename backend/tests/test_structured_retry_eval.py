import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_frozen_retry_evaluation_cli_reports_explicit_denominators(tmp_path):
    backend = Path(__file__).parents[1]
    output = tmp_path / "retry-evaluation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(backend / "scripts" / "structured_retry_eval.py"),
            "run",
            "--cases",
            str(backend / "evaluation" / "structured_retry_cases.json"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["case_count"] == 30
    assert report["cases_by_template"] == {"cloze": 10, "reading": 10, "single_choice": 10}
    assert report["provider_calls"] == 0
    assert set(report["arms"]) == {"single_attempt", "blind_retry", "feedback_retry"}
    packets = report["reference_review_packets"]
    assert len(packets) == len({packet["review_id"] for packet in packets}) == 30
    assert all(
        set(packet) == {"review_id", "content", "instructions", "rubric_version"}
        for packet in packets
    )
    for arm in report["arms"].values():
        assert arm["denominators"]["planned_cases"] == 30
        assert arm["counts"]["missing"] == 0
        assert (
            arm["counts"]["failed"]
            == arm["denominators"]["planned_cases"] - arm["counts"]["final_schema_success"]
        )
        assert arm["counts"]["cancelled"] == 0
        assert arm["counts"]["unresolved"] == 0
        assert arm["cost"]["complete"] is False
        assert arm["cost"]["total"] is None
    assert report["arms"]["single_attempt"]["counts"]["failed"] == 15
    assert report["arms"]["blind_retry"]["counts"]["recovered"] == 0
    assert report["arms"]["feedback_retry"]["counts"]["recovered"] == 15
    assert report["provenance"]["code_revision"]
    assert len(report["provenance"]["case_hashes"]) == 30
    assert report["limitations"]


@pytest.mark.parametrize("assessment_count", [0, 1])
def test_review_cli_writes_exploratory_interview_brief_with_coverage(tmp_path, assessment_count):
    backend = Path(__file__).parents[1]
    command = [sys.executable, str(backend / "scripts" / "structured_retry_eval.py")]
    evaluation = tmp_path / "evaluation.json"
    run = subprocess.run(
        command
        + [
            "run",
            "--cases",
            str(backend / "evaluation" / "structured_retry_cases.json"),
            "--output",
            str(evaluation),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(evaluation.read_text(encoding="utf-8"))
    packet = report["reference_review_packets"][0]
    assessment = {
        "review_id": packet["review_id"],
        "status": "reviewed",
        "reference_model": "test-reference",
        "rubric_version": packet["rubric_version"],
        "rubric": {
            dimension: {"verdict": "pass", "evidence": "Synthetic test evidence"}
            for dimension in [
                "answer_correctness",
                "explanation_correctness",
                "ambiguity",
                "knowledge_point_fit",
                "difficulty_fit",
            ]
        },
        "solved_answer": "A",
        "solve_reason": "Synthetic test explanation",
        "reference_pass": True,
        "judge_pass": True,
        "judge_model": "test-judge",
        "judge_rubric_version": "test-v1",
    }
    assessments = tmp_path / "assessments.jsonl"
    assessments.write_text(
        json.dumps(assessment) + "\n" if assessment_count else "", encoding="utf-8"
    )
    output = tmp_path / "reviewed.json"

    result = subprocess.run(
        command
        + [
            "review",
            "--evaluation",
            str(evaluation),
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
    reviewed = json.loads(output.read_text(encoding="utf-8"))
    brief = output.with_suffix(".md").read_text(encoding="utf-8")
    assert brief == reviewed["interview_brief"]
    assert "30 fixed inputs" in brief
    assert "10 cloze, 10 reading, 10 single_choice" in brief
    assert "single_attempt: schema-valid 15/30" in brief
    assert "feedback_retry: schema-valid 30/30" in brief
    assert f"assessments {assessment_count}/30" in brief
    assert f"pending {30 - assessment_count}/30" in brief
    assert f"confirmed reference passes {assessment_count}/{assessment_count}" in brief
    assert "equal-budget blind and feedback retries" in brief
    assert "exploratory" in brief
    assert "scripted" in brief
    assert "not human accuracy" in brief
    assert "no measured production improvement" in brief
    assert "Owner-confirmed personal contribution: pending" in brief
    assert "Cost: unknown" in brief
