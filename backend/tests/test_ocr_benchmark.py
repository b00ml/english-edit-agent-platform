"""Benchmark metrics are deliberately narrower than OCR/content accuracy."""

from scripts.ocr_benchmark import anchor_cer, measure_output, normalize_text, table_pair_checks


def test_normalization_ignores_formatting_but_keeps_negation_and_digits():
    assert normalize_text("<b>He doesn’t</b> **work** 12") == "hedoesn'twork12"
    assert normalize_text("doesn't") != normalize_text("does")


def test_anchor_measure_is_best_substring_not_whole_page_score():
    assert anchor_cer("He works.", "Heading. He works. Footer.") == 0
    assert 0 < anchor_cer("He works.", "He work.") < 0.2
    assert anchor_cer("He works.", "") == 1


def test_unrelated_output_does_not_get_perfect_anchor_coverage():
    result = measure_output("purple ocean", ["He does not work."])
    assert result["anchor_near"] == 0 and result["anchor_exact"] == 0
    assert "not full-page" in result["warning"]


def test_html_table_relation_requires_same_row():
    value = (
        "<table><tr><td>house</td><td>房屋</td></tr><tr><td>home</td><td>家庭成员</td></tr></table>"
    )
    assert table_pair_checks(value, [["house", "房屋"], ["house", "家庭成员"]]) == [True, False]
    assert table_pair_checks("house means 房屋", [["house", "房屋"]]) == [False]


def test_markdown_table_relation_is_not_anywhere_on_page():
    value = "| word | meaning |\n| --- | --- |\n| house | 房屋 |\n| home | 家庭成员 |"
    assert table_pair_checks(value, [["house", "房屋"], ["house", "家庭成员"]]) == [True, False]


def make_saved_report(tmp_path):
    import json

    (tmp_path / "S01").mkdir()
    (tmp_path / "S01" / "output.md").write_text(
        "He works.\n| word | meaning |\n| --- | --- |\n| house | 房屋 |", encoding="utf-8"
    )
    report = {
        "engine": "mineru",
        "package_version": "test",
        "samples": [
            {"sample_id": "S01", "status": "ok", "seconds": 9.0, "anchor_exact": 0},
            {"sample_id": "S02", "status": "failed", "seconds": 1.0},
        ],
    }
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps({"S01": {"anchors": ["He works."], "table_pairs": [["house", "房屋"]]}}),
        encoding="utf-8",
    )
    return gold


def test_rescore_preserves_timings_records_gold_and_excludes_failures(tmp_path):
    import hashlib

    from scripts.ocr_benchmark import rescore_report, summarize_report

    gold = make_saved_report(tmp_path)
    report = rescore_report(tmp_path, gold)
    assert report["gold_sha256"] == hashlib.sha256(gold.read_bytes()).hexdigest()
    assert report["samples"][0]["seconds"] == 9.0
    summary = summarize_report(report)
    assert summary["pages"] == 2 and summary["ok"] == 1
    assert summary["anchors"] == summary["anchor_exact"] == 1
    assert summary["table_pairs_preserved"] == 1
    assert summary["seconds_sum_pages"] == 9.0
    assert summary["median_seconds_after_first_page"] is None


def test_rescore_missing_output_does_not_overwrite_report(tmp_path):
    import pytest

    from scripts.ocr_benchmark import rescore_report

    gold = make_saved_report(tmp_path)
    before = (tmp_path / "report.json").read_bytes()
    (tmp_path / "S01" / "output.md").unlink()
    with pytest.raises(FileNotFoundError):
        rescore_report(tmp_path, gold)
    assert (tmp_path / "report.json").read_bytes() == before


def test_score_only_cli_never_initializes_engine(tmp_path, monkeypatch, capsys):
    from scripts import ocr_benchmark

    gold = make_saved_report(tmp_path)

    def forbidden(options):
        raise AssertionError("Scoring must never initialize OCR")

    monkeypatch.setattr(ocr_benchmark, "make_mineru", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ocr_benchmark",
            "--engine",
            "mineru",
            "--output",
            str(tmp_path),
            "--gold",
            str(gold),
            "--score-only",
        ],
    )
    assert ocr_benchmark.main() == 1  # saved failure remains a failure
    assert '"anchor_exact": 1' in capsys.readouterr().out


def test_paddle_export_never_appends_previous_aggregate(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts.ocr_benchmark import make_paddle

    class Result:
        def save_to_json(self, destination):
            pass

        def save_to_markdown(self, destination):
            (tmp_path / "native.md").write_text("fresh text", encoding="utf-8")

    class Pipeline:
        def __init__(self, **options):
            pass

        def predict(self, **options):
            return iter([Result()])

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PPStructureV3=Pipeline))
    (tmp_path / "output.md").write_text("previous aggregate", encoding="utf-8")
    result = make_paddle({})(tmp_path / "page.png", tmp_path)
    assert result["markdown"] == "fresh text"
    assert (tmp_path / "output.md").read_text(encoding="utf-8") == "fresh text"
