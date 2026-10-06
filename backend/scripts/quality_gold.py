"""Export annotation candidates or validate/report a human-reviewed JSONL dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.engine.gold_eval import GoldCase, gold_report  # noqa: E402
from app.versioning import hash_value  # noqa: E402


def export_candidates(output: Path, version: str, limit: int) -> None:
    from app.database import SessionLocal
    from app.models import ContentItem, GenerationTask, QualityRecord, TraceLog

    with SessionLocal() as db:
        cases = []
        items = (
            db.query(ContentItem)
            .order_by(ContentItem.created_at.asc(), ContentItem.id.asc())
            .limit(limit)
            .all()
        )
        for item in items:
            record = (
                db.query(QualityRecord)
                .filter(QualityRecord.item_id == item.id, QualityRecord.source == "auto")
                .order_by(QualityRecord.created_at.desc())
                .first()
            )
            task = db.get(GenerationTask, item.task_id)
            if record is None or task is None:
                continue
            snapshot = record.config_snapshot or {}
            traces = (
                db.query(TraceLog)
                .filter(
                    TraceLog.task_id == task.id,
                    TraceLog.stage == "generate",
                    TraceLog.success.is_(True),
                )
                .order_by(TraceLog.created_at.desc())
                .all()
            )
            # Exactly identify this content's successful generation, not another item in the batch.
            matching = [
                trace for trace in traces if item.thread_id and trace.trace_id == item.thread_id
            ]
            model = matching[0].model if matching else None
            cases.append(
                GoldCase(
                    case_id=item.id,
                    dataset_version=version,
                    template_id=item.template_id,
                    params=snapshot.get("generation_constraints", {}),
                    payload=item.payload,
                    payload_hash=hash_value(item.payload),
                    versions={
                        **snapshot,
                        "judge_model": snapshot.get("model_name"),
                        "model_name": model,
                        "threshold_verified": snapshot.get("threshold") is not None,
                    },
                    rag_provenance=item.provenance or {},
                    auto_score=record.score,
                    threshold=(
                        snapshot.get("threshold")
                        if snapshot.get("threshold") is not None
                        else settings.QUALITY_THRESHOLD
                    ),
                ).model_dump(mode="json")
            )
        with output.open("x", encoding="utf-8") as file:
            file.write("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--dataset-version", required=True)
    export.add_argument("--limit", type=int, default=100)
    report = sub.add_parser("report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        if args.limit < 1:
            parser.error("limit must be positive")
        export_candidates(args.output, args.dataset_version, args.limit)
    else:
        cases = [
            GoldCase.model_validate_json(line)
            for line in args.input.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
        result = gold_report(cases)
        with args.output.open("x", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
