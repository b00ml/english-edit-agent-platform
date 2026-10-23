from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.worker.benchmark_metrics import summarize_samples
from scripts import worker_benchmark


def test_summary_keeps_failures_in_denominator_and_reports_success_throughput():
    samples = [
        {
            "status": "succeeded",
            "queue_latency_seconds": 1.0,
            "end_to_end_seconds": 3.0,
            "worker_rss_bytes": 100,
            "worker_cpu_seconds": 0.5,
        },
        {
            "status": "failed",
            "queue_latency_seconds": 2.0,
            "end_to_end_seconds": 4.0,
            "worker_rss_bytes": None,
            "worker_cpu_seconds": None,
        },
    ]

    summary = summarize_samples(samples, elapsed_seconds=4.0)

    assert summary == {
        "submitted": 2,
        "succeeded": 1,
        "failed": 1,
        "unresolved": 0,
        "failure_rate": 0.5,
        "failure_rate_denominator": 2,
        "unresolved_rate": 0.0,
        "unresolved_rate_denominator": 2,
        "successful_throughput_per_hour": 900.0,
        "queue_latency_sample_count": 2,
        "queue_latency_p95_seconds": 2.0,
        "end_to_end_sample_count": 2,
        "end_to_end_p95_seconds": 4.0,
        "worker_rss_sample_count": 1,
        "worker_rss_max_bytes": 100,
        "worker_cpu_sample_count": 1,
        "worker_cpu_seconds_total": 0.5,
    }


def test_empty_sample_summary_has_no_division_or_invented_percentiles():
    summary = summarize_samples([], elapsed_seconds=0.0)

    assert summary["submitted"] == 0
    assert summary["failure_rate"] is None
    assert summary["successful_throughput_per_hour"] is None
    assert summary["queue_latency_p95_seconds"] is None
    assert summary["end_to_end_p95_seconds"] is None


def test_runner_reports_infrastructure_only_and_keeps_task_failures(monkeypatch):
    class Result:
        id = "task-1"

        def ready(self):
            return True

        def get(self, timeout):
            raise RuntimeError("stub failure")

    class Celery:
        def send_task(self, *args, **kwargs):
            return Result()

    monkeypatch.setattr(worker_benchmark, "celery_app", Celery())
    monkeypatch.setattr(worker_benchmark.time, "sleep", lambda _: None)
    args = worker_benchmark.argparse.Namespace(
        tasks=1,
        worker_concurrency=2,
        work_seconds=0.0,
        fail_every=0,
        timeout_seconds=1.0,
        poll_seconds=0.001,
        output=None,
    )

    report = worker_benchmark.run_benchmark(args)

    assert report["benchmark_kind"] == "celery_redis_worker_infrastructure_stub"
    assert report["provider_calls"] == 0
    assert report["metrics"]["submitted"] == 1
    assert report["metrics"]["failed"] == 1
    assert report["metrics"]["failure_rate"] == 1.0
    assert report["configuration"]["worker_concurrency"] == 2


def test_runner_keeps_timed_out_submissions_unresolved_in_submitted_denominator(monkeypatch):
    class Result:
        id = "task-timeout"

        def ready(self):
            return False

    class Celery:
        def send_task(self, *args, **kwargs):
            return Result()

    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(worker_benchmark, "celery_app", Celery())
    monkeypatch.setattr(worker_benchmark.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker_benchmark.time, "sleep", lambda _: None)
    args = worker_benchmark.argparse.Namespace(
        tasks=1,
        worker_concurrency=1,
        work_seconds=0.0,
        fail_every=0,
        timeout_seconds=1.0,
        poll_seconds=0.001,
        output=None,
    )

    report = worker_benchmark.run_benchmark(args)

    assert report["samples"][0]["status"] == "timed_out"
    assert report["metrics"]["submitted"] == 1
    assert report["metrics"]["failed"] == 0
    assert report["metrics"]["unresolved"] == 1
    assert report["metrics"]["failure_rate"] == 0.0
    assert report["metrics"]["failure_rate_denominator"] == 1
    assert report["metrics"]["unresolved_rate"] == 1.0
    assert report["metrics"]["unresolved_rate_denominator"] == 1


def test_stub_cli_returns_incomplete_when_work_is_unresolved(monkeypatch):
    monkeypatch.setattr(
        worker_benchmark, "parse_args", lambda: SimpleNamespace(arm="stub", output=None)
    )
    monkeypatch.setattr(
        worker_benchmark,
        "run_benchmark",
        lambda _: {"metrics": summarize_samples([{"status": "timed_out"}], 1.0)},
    )

    assert worker_benchmark.main() == 1


def test_real_arm_requires_explicit_fixed_profile_configuration(monkeypatch):
    monkeypatch.setattr(
        worker_benchmark.sys,
        "argv",
        [
            "worker_benchmark.py",
            "--arm",
            "real",
            "--worker-concurrency",
            "5",
            "--real-profile",
            "fixed",
            "--real-template-id",
            "template",
            "--real-user-id",
            "admin",
            "--real-params",
            "{}",
        ],
    )

    try:
        worker_benchmark.parse_args()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("real arm must refuse to run without fixed profile configuration")


def test_http_generate_enqueue_measures_request_and_keeps_token_out_of_report(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"task_id": "task-1"}

    class Client:
        def post(self, url, *, json, headers):
            assert url == "http://localhost:8000/api/generate"
            assert headers == {"Authorization": "Bearer secret-token"}
            return Response()

    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(worker_benchmark.time, "monotonic", lambda: next(ticks))

    task_id, elapsed = worker_benchmark.enqueue_real_task(
        Client(),
        base_url="http://localhost:8000",
        token="secret-token",
        payload={"template_id": "template", "params": {}, "quantity": 1},
    )

    assert task_id == "task-1"
    assert elapsed == 0.25
    report = {
        "configuration": worker_benchmark.real_benchmark_configuration(
            worker_benchmark.argparse.Namespace(
                real_profile="fixed",
                real_template_id="template",
                real_runs=1,
                real_items_per_run=1,
                timeout_seconds=30,
                real_api_base_url="http://localhost:8000/",
            )
        ),
        "metrics": worker_benchmark.summarize_real_samples(
            [{"submitted_items": 1, "successful_items": 0, "api_enqueue_seconds": elapsed}]
        ),
    }
    assert report["metrics"]["api_enqueue_p95_seconds"] == 0.25
    assert report["configuration"]["api_enqueue_measurement"] == (
        "authenticated HTTP POST /api/generate round-trip"
    )
    assert "secret-token" not in str(report)


@pytest.mark.parametrize("failure_type", ["ConnectError", "ReadTimeout"])
def test_http_enqueue_failure_preserves_request_latency_without_exposing_token(
    monkeypatch, failure_type
):
    class Client:
        def post(self, *args, **kwargs):
            raise getattr(worker_benchmark.httpx, failure_type)("request failed")

    ticks = iter([20.0, 20.4])
    monkeypatch.setattr(worker_benchmark.time, "monotonic", lambda: next(ticks))

    try:
        worker_benchmark.enqueue_real_task(
            Client(),
            base_url="http://localhost:8000",
            token="secret-token",
            payload={},
        )
    except worker_benchmark.RealEnqueueFailure as exc:
        assert round(exc.elapsed_seconds, 2) == 0.4
        assert exc.failure_type == failure_type
        assert "secret-token" not in str(exc)
        sample = worker_benchmark.real_enqueue_failure_sample(exc, item_count=2)
        metrics = worker_benchmark.summarize_real_samples([sample])
        assert sample["status"] == "enqueue_failed"
        assert sample["api_enqueue_seconds"] == exc.elapsed_seconds
        assert metrics["sample_count"] == 2
        assert metrics["failed_items"] == 0
        assert metrics["failure_rate"] == 0.0
        assert metrics["unresolved_items"] == 2
        assert metrics["enqueue_error_run_count"] == 1
        assert metrics["enqueue_error_rate_denominator"] == 1
        assert metrics["api_enqueue_sample_count"] == 1
        assert "secret-token" not in str(sample)
    else:
        raise AssertionError("failed HTTP request must remain a failed enqueue sample")


def test_real_arm_rejects_empty_api_url_or_token_before_request(monkeypatch):
    base_argv = [
        "worker_benchmark.py",
        "--arm",
        "real",
        "--worker-concurrency",
        "5",
        "--real-profile",
        "fixed",
        "--real-template-id",
        "template",
        "--real-user-id",
        "admin",
        "--real-params",
        "{}",
        "--real-api-base-url",
        "http://localhost:8000",
        "--real-api-token",
        "secret-token",
    ]
    for flag in ("--real-api-base-url", "--real-api-token"):
        argv = base_argv.copy()
        argv[argv.index(flag) + 1] = ""
        monkeypatch.setattr(worker_benchmark.sys, "argv", argv)
        try:
            worker_benchmark.parse_args()
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"real arm must reject an empty {flag}")


def test_real_summary_separates_failure_and_unresolved_items():
    metrics = worker_benchmark.summarize_real_samples(
        [
            {
                "status": "partially_succeeded",
                "submitted_items": 4,
                "successful_items": 1,
                "failed_items": 1,
                "cancelled_items": 0,
                "unresolved_items": 2,
                "api_enqueue_seconds": 0.1,
            },
            {
                "status": "enqueue_failed",
                "submitted_items": 2,
                "successful_items": 0,
                "failed_items": 0,
                "cancelled_items": 0,
                "unresolved_items": 2,
                "api_enqueue_seconds": 0.3,
            },
        ]
    )

    assert metrics["submitted_items"] == 6
    assert metrics["successful_items"] == 1
    assert metrics["failed_items"] == 1
    assert metrics["cancelled_items"] == 0
    assert metrics["unresolved_items"] == 4
    assert metrics["failure_rate"] == 1 / 6
    assert metrics["unresolved_rate"] == 4 / 6
    assert metrics["api_enqueue_sample_count"] == 2


def test_real_p95_uses_run_samples_and_empty_input_is_null():
    assert worker_benchmark._p95([1.0, 2.0, 3.0]) == 3.0
    assert worker_benchmark._p95([]) is None


def test_real_summary_marks_partial_item_cost_coverage_unknown():
    metrics = worker_benchmark.summarize_real_samples(
        [
            {
                "submitted_items": 2,
                "successful_items": 1,
                "failed_items": 1,
                "cancelled_items": 0,
                "unresolved_items": 0,
                "api_enqueue_seconds": 0.1,
                "submit_to_completion_seconds": 2.0,
                "known_estimated_trace_cost_subtotal": 0.04,
                "estimated_trace_cost": None,
                "cost_sample_count": 3,
                "cost_unknown_trace_count": 1,
                "cost_covered_items": 1,
                "cost_unknown_items": 1,
            },
            {
                "submitted_items": 1,
                "successful_items": 0,
                "failed_items": 0,
                "cancelled_items": 0,
                "unresolved_items": 1,
                "api_enqueue_seconds": 0.2,
                "submit_to_completion_seconds": None,
                "estimated_trace_cost": None,
                "cost_sample_count": 0,
                "cost_unknown_trace_count": 0,
                "cost_covered_items": 0,
                "cost_unknown_items": 1,
            },
        ]
    )

    assert metrics["submitted_items"] == 3
    assert metrics["successful_items"] == 1
    assert metrics["failed_items"] == 1
    assert metrics["unresolved_items"] == 1
    assert metrics["failure_rate"] == 1 / 3
    assert metrics["api_enqueue_sample_count"] == 2
    assert metrics["submit_to_completion_sample_count"] == 1
    assert metrics["estimated_trace_cost_sample_count"] == 3
    assert metrics["estimated_trace_cost_unknown_sample_count"] == 1
    assert metrics["cost_covered_items"] == 1
    assert metrics["cost_unknown_items"] == 2
    assert metrics["cost_coverage_complete"] is False
    assert metrics["cost_covered_runs"] == 0
    assert metrics["cost_unknown_runs"] == 2
    assert metrics["known_estimated_trace_cost_subtotal"] == 0.04
    assert metrics["estimated_trace_cost_total"] is None
    assert metrics["estimated_per_item_cost"] is None
    assert metrics["billed_cost"] is None


@pytest.mark.parametrize("task_status", ["running", "partially_succeeded", None])
def test_real_runner_observes_items_and_cost_coverage_even_at_timeout(monkeypatch, task_status):
    report = _real_report_with_snapshot(
        monkeypatch,
        task_status=task_status,
        statuses=["succeeded", "failed", "cancelled", "running"],
        quantity=5,
        traces=[("task-1:0", 0.04, True), ("task-1:1", None, True), ("task-1:3", 0.02, True)],
    )

    sample = report["samples"][0]
    assert sample["status"] == (
        task_status
        if task_status == "partially_succeeded"
        else ("timed_out" if task_status else "missing_task")
    )
    assert sample["successful_items"] == 1
    assert sample["failed_items"] == 1
    assert sample["cancelled_items"] == 1
    assert sample["unresolved_items"] == 2
    assert sample["missing_items"] == 1
    assert sample["estimated_trace_cost"] is None
    assert sample["cost_covered_items"] == 1
    assert sample["cost_unknown_items"] == 4
    assert sample["cost_unknown_trace_count"] == 1
    assert report["metrics"]["failure_rate"] == 1 / 5
    assert report["metrics"]["known_estimated_trace_cost_subtotal"] == 0.06
    assert report["metrics"]["estimated_per_item_cost"] is None
    if task_status != "partially_succeeded":
        assert report["metrics"]["submit_to_completion_sample_count"] == 0


def test_real_summary_reports_cost_only_when_every_item_is_covered():
    metrics = worker_benchmark.summarize_real_samples(
        [
            {
                "submitted_items": 2,
                "successful_items": 1,
                "failed_items": 1,
                "cost_covered_items": 2,
                "known_estimated_trace_cost_subtotal": 0.06,
                "estimated_trace_cost": 0.06,
                "cost_sample_count": 3,
            }
        ]
    )

    assert metrics["cost_coverage_complete"] is True
    assert metrics["cost_covered_runs"] == 1
    assert metrics["cost_unknown_runs"] == 0
    assert metrics["cost_unknown_items"] == 0
    assert metrics["estimated_trace_cost_total"] == 0.06
    assert metrics["estimated_per_item_cost"] == 0.03


@pytest.mark.parametrize("usage_reported", [True, False, None])
def test_real_cost_zero_requires_reported_usage(monkeypatch, usage_reported):
    report = _real_report_with_snapshot(
        monkeypatch,
        task_status="succeeded",
        statuses=["succeeded"],
        quantity=1,
        traces=[("task-1:0", 0.0, usage_reported)],
    )

    metrics = report["metrics"]
    assert metrics["cost_covered_items"] == int(usage_reported is True)
    assert metrics["cost_unknown_items"] == int(usage_reported is not True)
    assert metrics["estimated_trace_cost_unknown_sample_count"] == int(usage_reported is not True)
    assert metrics["cost_coverage_complete"] is (usage_reported is True)
    assert metrics["estimated_per_item_cost"] == (0.0 if usage_reported is True else None)
    assert metrics["known_estimated_trace_cost_subtotal"] == (
        0.0 if usage_reported is True else None
    )


@pytest.mark.parametrize("stage", ["queue_dispatch", "queue_item", "workflow", "rag", "rag_rerank"])
def test_real_cost_excludes_lifecycle_but_keeps_unknown_rag_call(monkeypatch, stage):
    report = _real_report_with_snapshot(
        monkeypatch,
        task_status="succeeded",
        statuses=["succeeded"],
        quantity=1,
        traces=[
            ("task-1:0", 0.04, True, "generate"),
            ("task-1:0", 0.01, True, "rag_plan"),
            ("task-1:0", 0.0, None, stage),
        ],
    )

    metrics = report["metrics"]
    complete = stage != "rag_rerank"
    assert metrics["cost_coverage_complete"] is complete
    assert metrics["cost_covered_items"] == int(complete)
    assert metrics["known_estimated_trace_cost_subtotal"] == pytest.approx(0.05)
    assert metrics["estimated_trace_cost_unknown_sample_count"] == int(not complete)
    assert metrics["estimated_per_item_cost"] == (pytest.approx(0.05) if complete else None)


@pytest.mark.parametrize("naive_timestamps", [False, True])
def test_real_completion_duration_starts_before_http_submission(monkeypatch, naive_timestamps):
    report = _real_report_with_snapshot(
        monkeypatch,
        task_status="succeeded",
        statuses=["succeeded"],
        quantity=1,
        traces=[],
        naive_timestamps=naive_timestamps,
    )

    assert report["samples"][0]["submit_to_completion_seconds"] == 5.0
    assert report["metrics"]["submit_to_completion_p95_seconds"] == 5.0


def _real_report_with_snapshot(
    monkeypatch, *, task_status, statuses, quantity, traces, naive_timestamps=False
):
    from app.models import GenerationTaskItem

    now = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
    task_time = now.replace(tzinfo=None) if naive_timestamps else now
    task = (
        SimpleNamespace(
            id="task-1",
            status=task_status,
            created_at=task_time,
            updated_at=task_time + timedelta(seconds=3),
        )
        if task_status
        else None
    )
    items = [
        SimpleNamespace(status=status, thread_id=f"task-1:{index}")
        for index, status in enumerate(statuses)
    ]
    traces = [trace if len(trace) == 4 else (*trace, "generate") for trace in traces]

    class Query:
        def __init__(self, entity):
            self.entity = entity

        def filter(self, *conditions):
            return self

        def first(self):
            return task

        def all(self):
            return (
                items
                if self.entity[0] is GenerationTaskItem
                else [trace[: len(self.entity)] for trace in traces]
            )

    class Database:
        def query(self, *entities):
            return Query(entities)

        def expire_all(self):
            pass

        def close(self):
            pass

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(
        worker_benchmark,
        "datetime",
        SimpleNamespace(now=lambda tz: now - timedelta(seconds=2)),
    )
    monkeypatch.setattr("app.database.SessionLocal", Database)
    monkeypatch.setattr(worker_benchmark, "preflight_real_profile", lambda *args: None)
    monkeypatch.setattr(
        worker_benchmark, "enqueue_real_task", lambda *args, **kwargs: ("task-1", 0.1)
    )
    monkeypatch.setattr(worker_benchmark.httpx, "Client", Client)
    monkeypatch.setattr(worker_benchmark.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker_benchmark.time, "sleep", lambda _: None)
    args = worker_benchmark.argparse.Namespace(
        real_template_id="template",
        real_profile="fixed",
        real_user_id="admin",
        real_runs=1,
        real_api_base_url="http://localhost:8000",
        real_api_token="secret",
        real_params={},
        real_items_per_run=quantity,
        timeout_seconds=1.0,
        poll_seconds=0.1,
    )

    return worker_benchmark.run_real_benchmark(args)
