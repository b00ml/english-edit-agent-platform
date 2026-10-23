import json
import subprocess
import sys
from pathlib import Path


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
    for arm in report["arms"].values():
        assert arm["denominators"]["planned_cases"] == 30
        assert arm["counts"]["missing"] == 0
        assert arm["counts"]["failed"] == arm["denominators"]["planned_cases"] - arm["counts"]["final_schema_success"]
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
