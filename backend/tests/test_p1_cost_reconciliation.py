from decimal import Decimal

import pytest

from scripts.reconcile_costs import compare_costs, read_bills


def test_reconciliation_reports_missing_and_different_estimates_without_inventing_values():
    report = compare_costs(
        {"a": Decimal(".5"), "b": Decimal("1")}, {"a": Decimal(".6"), "c": Decimal(".3")}
    )
    assert {row["trace_id"]: row["status"] for row in report["records"]} == {
        "a": "mismatch",
        "b": "missing_bill",
        "c": "missing_trace",
    }
    assert report["issues"] == 3
    assert report["records"][2]["estimate"] is None


def test_reconciliation_aggregates_normalized_csv_without_rounding_float_money(tmp_path):
    path = tmp_path / "bill.csv"
    path.write_text("trace_id,billed_cost\na,0.1\na,0.2\n", encoding="utf-8")
    bills = read_bills(path)
    assert bills == {"a": Decimal(".3")}
    assert compare_costs({"a": Decimal(".3")}, bills)["issues"] == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_bill_costs_must_be_finite_nonnegative(tmp_path, value):
    path = tmp_path / "bill.csv"
    path.write_text(f"trace_id,billed_cost\na,{value}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_bills(path)
