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


def test_http_enqueue_failure_preserves_request_latency_without_exposing_token(monkeypatch):
    class Client:
        def post(self, *args, **kwargs):
            raise worker_benchmark.httpx.ConnectError("connection refused")

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
        assert exc.failure_type == "ConnectError"
        assert "secret-token" not in str(exc)
        sample = worker_benchmark.real_enqueue_failure_sample(exc, item_count=2)
        metrics = worker_benchmark.summarize_real_samples([sample])
        assert sample["status"] == "enqueue_failed"
        assert sample["api_enqueue_seconds"] == exc.elapsed_seconds
        assert metrics["sample_count"] == 2
        assert metrics["failed_items"] == 2
        assert metrics["failure_rate"] == 1.0
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


def test_real_summary_counts_http_enqueue_failure_in_item_denominator():
    metrics = worker_benchmark.summarize_real_samples(
        [
            {
                "status": "completed",
                "submitted_items": 2,
                "successful_items": 2,
                "api_enqueue_seconds": 0.1,
            },
            {
                "status": "enqueue_failed",
                "submitted_items": 2,
                "successful_items": 0,
                "failed_items": 2,
                "api_enqueue_seconds": 0.3,
            },
        ]
    )

    assert metrics["sample_count"] == 4
    assert metrics["failed_items"] == 2
    assert metrics["failure_rate"] == 0.5
    assert metrics["api_enqueue_sample_count"] == 2


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
