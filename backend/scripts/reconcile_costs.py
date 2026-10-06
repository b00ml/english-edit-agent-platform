"""Compare immutable Trace estimates with an explicitly normalized supplier bill.

The operator must map provider statements to trace_id and the same currency/window.
This tool never converts currencies, infers missing bills or contacts a provider.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def compare_costs(
    estimates: dict[str, Decimal],
    bills: dict[str, Decimal],
    tolerance: Decimal = Decimal("0.000001"),
) -> dict[str, object]:
    if tolerance < 0 or not tolerance.is_finite():
        raise ValueError("tolerance must be finite and non-negative")
    records = []
    for trace_id in sorted(estimates.keys() | bills.keys()):
        estimate, billed = estimates.get(trace_id), bills.get(trace_id)
        difference = billed - estimate if estimate is not None and billed is not None else None
        status = (
            "missing_trace"
            if estimate is None
            else (
                "missing_bill"
                if billed is None
                else "matched" if abs(difference) <= tolerance else "mismatch"
            )
        )
        records.append(
            {
                "trace_id": trace_id,
                "estimate": str(estimate) if estimate is not None else None,
                "billed": str(billed) if billed is not None else None,
                "difference": str(difference) if difference is not None else None,
                "status": status,
            }
        )
    return {
        "records": records,
        "estimated_total": str(sum(estimates.values(), Decimal(0))),
        "billed_total": str(sum(bills.values(), Decimal(0))),
        "issues": sum(record["status"] != "matched" for record in records),
        "warning": "币种、账期和 trace_id 映射由操作者提供；费用为估算，不替代供应商账单",
    }


def read_bills(path: Path) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"trace_id", "billed_cost"} <= set(reader.fieldnames or []):
            raise ValueError("bill CSV requires trace_id and billed_cost columns")
        for row in reader:
            trace_id = row["trace_id"].strip()
            cost = Decimal(row["billed_cost"])
            if not trace_id or not cost.is_finite() or cost < 0:
                raise ValueError("invalid bill trace_id or billed_cost")
            result[trace_id] = result.get(trace_id, Decimal(0)) + cost
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bills", type=Path, required=True)
    parser.add_argument(
        "--currency", required=True, help="operator-declared common currency, no conversion"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tolerance", type=Decimal, default=Decimal("0.000001"))
    args = parser.parse_args()
    bills = read_bills(args.bills)
    from app.database import SessionLocal
    from app.models import TraceLog

    with SessionLocal() as db:
        rows = db.query(TraceLog).filter(TraceLog.trace_id.in_(bills)).all()
        estimates: dict[str, Decimal] = {}
        for row in rows:
            estimates[row.trace_id] = estimates.get(row.trace_id, Decimal(0)) + Decimal(
                str(row.cost or 0)
            )
        unknown_usage = sum(
            row.stage in {"generate", "qc", "embedding"} and row.usage_reported is not True
            for row in rows
        )
    report = compare_costs(estimates, bills, args.tolerance)
    report["currency"] = args.currency
    report["unknown_usage_calls"] = unknown_usage
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    return 1 if report["issues"] or report["unknown_usage_calls"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
