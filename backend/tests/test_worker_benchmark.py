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
        "failure_rate": 0.5,
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


def test_runner_keeps_timed_out_submissions_in_failure_denominator(monkeypatch):
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
    assert report["metrics"]["failed"] == 1
    assert report["metrics"]["failure_rate"] == 1.0


def test_real_arm_requires_explicit_fixed_profile_configuration(monkeypatch):
    monkeypatch.setattr(
        worker_benchmark.sys,
        "argv",
        ["worker_benchmark.py", "--arm", "real", "--worker-concurrency", "5"],
    )

    try:
        worker_benchmark.parse_args()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("real arm must refuse to run without fixed profile configuration")


def test_real_p95_uses_run_samples_and_empty_input_is_null():
    assert worker_benchmark._p95([1.0, 2.0, 3.0]) == 3.0
    assert worker_benchmark._p95([]) is None


def test_real_summary_labels_estimated_cost_and_counts_failures_in_denominator():
    metrics = worker_benchmark.summarize_real_samples(
        [
            {
                "submitted_items": 2,
                "successful_items": 1,
                "api_enqueue_seconds": 0.1,
                "submit_to_completion_seconds": 2.0,
                "estimated_trace_cost": 0.04,
                "cost_sample_count": 2,
            },
            {
                "submitted_items": 1,
                "successful_items": 0,
                "api_enqueue_seconds": 0.2,
                "submit_to_completion_seconds": None,
                "estimated_trace_cost": None,
                "cost_sample_count": 0,
            },
        ]
    )

    assert metrics["sample_count"] == 3
    assert metrics["failed_items"] == 2
    assert metrics["failure_rate"] == 2 / 3
    assert metrics["api_enqueue_sample_count"] == 2
    assert metrics["submit_to_completion_sample_count"] == 1
    assert metrics["estimated_trace_cost_sample_count"] == 2
    assert metrics["estimated_per_item_cost"] == 0.04 / 2
    assert metrics["billed_cost"] is None
