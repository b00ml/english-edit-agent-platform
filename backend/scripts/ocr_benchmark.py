"""Local OCR/structure benchmark. Never embeds, calls a paid API, or writes a knowledge DB.

Run one engine at a time in its evaluation interpreter. Inputs, model choices and
outputs are explicit CLI/config values; private course outputs stay outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata
import json
import os
import re
import statistics
import time
import traceback
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


def normalize_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    value = value.replace("**", "").replace("`", "")
    value = unicodedata.normalize("NFKC", value).replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", "", value).casefold()


def anchor_cer(expected: str, observed: str) -> float:
    """Best-substring character edit rate, NOT whole-page OCR accuracy."""
    expected, observed = normalize_text(expected), normalize_text(observed)
    if not expected:
        raise ValueError("Expected anchor must not be empty")
    if expected in observed:
        return 0.0
    previous = [0] * (len(observed) + 1)
    for index, char in enumerate(expected, 1):
        current = [index]
        for offset, actual in enumerate(observed, 1):
            current.append(
                min(current[-1] + 1, previous[offset] + 1, previous[offset - 1] + (char != actual))
            )
        previous = current
    return min(previous) / len(expected)


def measure_output(markdown: str, anchors: list[str], threshold: float = 0.1) -> dict[str, Any]:
    scores = [anchor_cer(anchor, markdown) for anchor in anchors]
    return {
        "characters": len(markdown),
        "anchor_count": len(anchors),
        "anchor_exact": sum(value == 0 for value in scores),
        "anchor_near": sum(value <= threshold for value in scores),
        "anchor_cer": scores,
        "html_table_tags": len(re.findall(r"<table\b", markdown, re.I)),
        "markdown_table_separators": len(re.findall(r"(?m)^\s*\|?\s*:?-{3,}.*\|", markdown)),
        "warning": "Anchor coverage is not full-page character accuracy or table correctness.",
    }


class TableRows(HTMLParser):
    """Extract same-row text; presence anywhere on the page is not a table relation."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def table_pair_checks(markdown: str, pairs: list[list[str]]) -> list[bool]:
    collector = TableRows()
    collector.feed(markdown)
    rows = collector.rows
    for line in markdown.splitlines():
        if line.strip().startswith("|") and not re.match(r"^\s*\|[ :|-]+\|?\s*$", line):
            rows.append(re.split(r"(?<!\\)\|", line.strip().strip("|")))
    return [
        any(
            all(
                any(normalize_text(label) in normalize_text(cell) for cell in row) for label in pair
            )
            for row in rows
        )
        for pair in pairs
    ]


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Summarize successful pages only; cold loading/downloads are not throughput."""
    samples = report["samples"]
    ok = [sample for sample in samples if sample["status"] == "ok"]
    warm = [sample["seconds"] for sample in samples[1:] if sample["status"] == "ok"]
    return {
        "engine": report["engine"],
        "version": report.get("package_version"),
        "pages": len(samples),
        "ok": len(ok),
        "anchors": sum(sample["anchor_count"] for sample in ok),
        "anchor_exact": sum(sample["anchor_exact"] for sample in ok),
        "anchor_near": sum(sample["anchor_near"] for sample in ok),
        "table_pairs": sum(len(sample.get("table_pair_checks", [])) for sample in ok),
        "table_pairs_preserved": sum(sum(sample.get("table_pair_checks", [])) for sample in ok),
        "median_seconds_after_first_page": round(statistics.median(warm), 3) if warm else None,
        "seconds_sum_pages": round(sum(sample["seconds"] for sample in ok), 3),
        "exported_characters": sum(sample["characters"] for sample in ok),
    }


def rescore_report(output: Path, gold_path: Path) -> dict[str, Any]:
    """Re-score saved outputs without importing engines or changing inference timings."""
    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    gold_bytes = gold_path.read_bytes()
    gold = json.loads(gold_bytes.decode("utf-8"))
    for sample in report["samples"]:
        if sample["status"] != "ok":
            continue
        sample_id = sample["sample_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sample_id):
            raise ValueError("Sample ID must be a simple directory name")
        expected = gold[sample_id]
        if not expected.get("anchors"):
            raise ValueError(f"No reviewed anchors for {sample_id}")
        markdown = (output / sample_id / "output.md").read_text(encoding="utf-8")
        sample.update(measure_output(markdown, expected["anchors"]))
        sample["table_pair_checks"] = table_pair_checks(markdown, expected.get("table_pairs", []))
    report["gold_sha256"] = hashlib.sha256(gold_bytes).hexdigest()
    report["scoring_method"] = "normalized anchor best-substring CER <= 0.1; same-row table pairs"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def make_paddle(options: dict[str, Any]) -> Callable[[Path, Path], dict[str, Any]]:
    from paddleocr import PPStructureV3

    pipeline = PPStructureV3(**options)

    def convert(image: Path, folder: Path) -> dict[str, Any]:
        result = next(iter(pipeline.predict(input=str(image))))
        result.save_to_json(str(folder / "result.json"))
        result.save_to_markdown(str(folder))
        files = [file for file in folder.glob("*.md") if file.name != "output.md"]
        if not files:
            raise ValueError("PaddleOCR returned no Markdown output")
        markdown = "\n".join(file.read_text(encoding="utf-8") for file in files)
        (folder / "output.md").write_text(markdown, encoding="utf-8")
        return {"markdown": markdown}

    return convert


def make_docling(options: dict[str, Any]) -> Callable[[Path, Path], dict[str, Any]]:
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption

    pipeline = PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=options.get("do_table_structure", True),
        enable_remote_services=False,
        artifacts_path=Path(options["artifacts_path"]) if options.get("artifacts_path") else None,
        table_structure_options=TableStructureOptions(),
        accelerator_options=AcceleratorOptions(
            device=options.get("device", "cpu"), num_threads=options.get("num_threads", 2)
        ),
        ocr_options=RapidOcrOptions(mode=OcrMode.FULL_PAGE, lang=["ch"], scale=1.0),
    )
    pipeline.ocr_batch_size = pipeline.layout_batch_size = pipeline.table_batch_size = 1
    converter = DocumentConverter(
        format_options={InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline)}
    )

    def convert(image: Path, folder: Path) -> dict[str, Any]:
        result = converter.convert(image)
        markdown = result.document.export_to_markdown()
        result.document.save_as_json(folder / "result.json")
        (folder / "output.md").write_text(markdown, encoding="utf-8")
        return {
            "markdown": markdown,
            "tables": len(result.document.tables),
            "conversion_status": str(result.status),
        }

    return convert


def make_mineru(options: dict[str, Any]) -> Callable[[Path, Path], dict[str, Any]]:
    import subprocess
    import sys

    executable = (
        Path(sys.executable).with_name("mineru.exe")
        if os.name == "nt"
        else Path(sys.executable).with_name("mineru")
    )
    if options.get("remote"):
        raise ValueError("Remote inference is disabled for this benchmark")

    def convert(image: Path, folder: Path) -> dict[str, Any]:
        command = [
            str(executable),
            "parse",
            str(image),
            "--tier",
            str(options.get("tier", "basic")),
            "--force",
            "--wait",
            "300",
            "--limit",
            "100000",
            "--output",
            str(folder / "output.md"),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", timeout=360
        )
        (folder / "stdout.txt").write_text(result.stdout, encoding="utf-8")
        (folder / "stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(
                f"MinerU CLI failed (exit {result.returncode}); see local stderr.txt"
            )
        path = folder / "output.md"
        if not path.exists():
            raise ValueError("MinerU returned no Markdown file")
        return {"markdown": path.read_text(encoding="utf-8")}

    return convert


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["paddle", "docling", "mineru"], required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--score-only", action="store_true", help="Re-score saved output; no inference"
    )
    args = parser.parse_args()
    if args.score_only:
        if args.gold is None:
            parser.error("--gold is required for --score-only")
        existing = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        if existing["engine"] != args.engine:
            parser.error("--engine differs from the saved report")
        report = rescore_report(args.output, args.gold)
        print(json.dumps(summarize_report(report), ensure_ascii=False), flush=True)
        return 0 if report["samples"] and all(s["status"] == "ok" for s in report["samples"]) else 1
    if args.manifest is None or args.config is None:
        parser.error("--manifest and --config are required for inference")
    records = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.limit:
        records = records[: args.limit]
    config = json.loads(args.config.read_text(encoding="utf-8"))[args.engine]
    gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else {}
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "engine": args.engine,
        "config": config,
        "samples": [],
        "inference": "local",
        "input_kind": "rendered_page_png_1800px",
        "full_page_gold": False,
    }
    maker = {"paddle": make_paddle, "docling": make_docling, "mineru": make_mineru}[args.engine]
    try:
        convert = maker(config)
        report["initialization_seconds"] = round(time.perf_counter() - started, 3)
        for record in records:
            folder = args.output / record["sample_id"]
            folder.mkdir(exist_ok=True)
            sample_start = time.perf_counter()
            entry = {"sample_id": record["sample_id"], "source_page": record["page_no"]}
            try:
                result = convert(Path(record["image"]), folder)
                markdown = result.pop("markdown")
                entry.update(
                    status="ok",
                    **result,
                    **measure_output(
                        markdown, gold.get(record["sample_id"], {}).get("anchors", [])
                    ),
                    table_pair_checks=table_pair_checks(
                        markdown, gold.get(record["sample_id"], {}).get("table_pairs", [])
                    ),
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - benchmark records per-sample failure, never claims success
                entry.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:600])
                (folder / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            entry["seconds"] = round(time.perf_counter() - sample_start, 3)
            report["samples"].append(entry)
            (args.output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    except (
        Exception
    ) as exc:  # initialization failure is recorded, not a completed quality comparison
        report.update(
            status="initialization_failed", error_type=type(exc).__name__, error=str(exc)[:1000]
        )
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    package = {"paddle": "paddleocr", "docling": "docling", "mineru": "mineru"}[args.engine]
    report["package_version"] = importlib.metadata.version(package)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps({k: v for k, v in report.items() if k != "samples"}, ensure_ascii=False),
        flush=True,
    )
    return (
        0 if report["samples"] and all(item["status"] == "ok" for item in report["samples"]) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
