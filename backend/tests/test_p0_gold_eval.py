from copy import deepcopy
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.engine.gold_eval import GoldCase, gold_report
from app.versioning import hash_value


def case_data():
    payload = {"stem": "She ___ daily.", "options": ["go", "goes"], "answer": "B"}
    annotation = {
        "reviewer": "human1",
        "human_attested": True,
        "payload_hash": hash_value(payload),
        "annotated_at": datetime.fromisoformat("2026-10-04T00:00:00+00:00"),
        "answer_correct": True,
        "explanation_correct": True,
        "unambiguous": True,
        "knowledge_match": True,
        "difficulty_match": True,
        "expected_answer": "B",
        "evidence": "Synthetic test only",
    }
    return {
        "case_id": "one",
        "dataset_version": "test-fixture",
        "status": "reviewed",
        "template_id": "single_choice",
        "params": {"difficulty": "易"},
        "payload": payload,
        "payload_hash": hash_value(payload),
        "versions": {
            "threshold_verified": True,
            **{
                key: "test-only"
                for key in (
                    "template_hash",
                    "prompt_hash",
                    "skill_hash",
                    "model_name",
                    "judge_model",
                    "judge_prompt_hash",
                    "judge_skill_hash",
                )
            },
        },
        "auto_score": 75,
        "threshold": 70,
        "annotations": [annotation, {**annotation, "reviewer": "human2"}],
    }


def test_unreviewed_candidates_cannot_produce_quality_report():
    data = case_data()
    data.update(status="candidate", annotations=[])
    with pytest.raises(ValueError, match="人工标注"):
        gold_report([GoldCase.model_validate(data)])


@pytest.mark.parametrize(
    "change",
    ["same_reviewer", "missing_version", "stale_payload", "disagreement", "fake_attestation"],
)
def test_gold_integrity_guards(change):
    data = case_data()
    if change == "same_reviewer":
        data["annotations"][1]["reviewer"] = "human1"
    if change == "missing_version":
        data["versions"].pop("judge_model")
    if change == "stale_payload":
        data["payload"]["answer"] = "A"
    if change == "disagreement":
        data["annotations"][1]["answer_correct"] = False
    if change == "fake_attestation":
        data["annotations"][0]["human_attested"] = False
    with pytest.raises(ValidationError):
        GoldCase.model_validate(data)


def test_gold_report_uses_per_case_threshold_and_human_truth():
    first = case_data()
    second = deepcopy(first)
    second.update(case_id="two", auto_score=65, threshold=60)
    for annotation in second["annotations"]:
        annotation["answer_correct"] = False
    report = gold_report([GoldCase.model_validate(first), GoldCase.model_validate(second)])
    assert report["overall"]["fp"] == 1
    assert report["overall"]["answer_error_rate"] == 0.5
    assert report["overall"]["false_pass_rate"] == 0.5
    assert report["small_sample_warning"]


def test_gold_cli_report_roundtrip_without_real_llm(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    data = GoldCase.model_validate(case_data()).model_dump(mode="json")
    source = tmp_path / "fixture.jsonl"
    output = tmp_path / "fixture-report.json"
    source.write_text(json.dumps(data) + "\n", encoding="utf-8")
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "scripts" / "quality_gold.py"),
        "report",
        "--input",
        str(source),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["overall"]["tp"] == 1
    assert subprocess.run(command, capture_output=True, text=True, timeout=30).returncode != 0


def test_disputed_answers_require_third_reviewer_even_with_equal_boolean_labels():
    data = case_data()
    data["annotations"][1]["expected_answer"] = "A"
    with pytest.raises(ValidationError, match="仲裁"):
        GoldCase.model_validate(data)
    data["adjudication"] = {**data["annotations"][0], "reviewer": "human3"}
    assert GoldCase.model_validate(data).truth().reviewer == "human3"
