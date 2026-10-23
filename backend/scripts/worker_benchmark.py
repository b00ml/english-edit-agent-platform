"""Measure Celery/Redis worker infrastructure with a non-production stub task.

Run after starting the normal Celery worker. This harness does not create
GenerationTask rows, run a model, or measure provider quality or cost.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.worker.benchmark_metrics import summarize_samples  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Celery worker infrastructure benchmark")
    parser.add_argument("--arm", choices=("stub", "real"), default="stub")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--worker-concurrency", type=int, required=True)
    parser.add_argument("--work-seconds", type=float, default=0.05)
    parser.add_argument("--fail-every", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--poll-seconds", type=float, default=0.2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--real-profile")
    parser.add_argument("--real-template-id")
    parser.add_argument("--real-user-id")
    parser.add_argument("--real-params", help="JSON object of valid template parameters")
    parser.add_argument("--real-api-base-url")
    parser.add_argument("--real-api-token")
    parser.add_argument("--real-runs", type=int, default=5)
    parser.add_argument("--real-items-per-run", type=int, default=1)
    args = parser.parse_args()
    if args.worker_concurrency < 1:
        parser.error("--worker-concurrency must be positive")
    if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
        parser.error("timeout and poll intervals must be positive")
    if args.arm == "real":
        missing = [
            flag
            for flag, value in (
                ("--real-profile", args.real_profile),
                ("--real-template-id", args.real_template_id),
                ("--real-user-id", args.real_user_id),
                ("--real-params", args.real_params),
                ("--real-api-base-url", args.real_api_base_url),
                ("--real-api-token", args.real_api_token),
            )
            if not value
        ]
        if missing:
            parser.error(f"--arm real requires configured values: {', '.join(missing)}")
        api_url = urlsplit(args.real_api_base_url)
        if api_url.scheme not in {"http", "https"} or not api_url.netloc:
            parser.error("--real-api-base-url must be an absolute HTTP(S) URL")
        try:
            args.real_params = json.loads(args.real_params)
        except json.JSONDecodeError as exc:
            parser.error(f"--real-params must be valid JSON: {exc}")
        if not isinstance(args.real_params, dict):
            parser.error("--real-params must decode to a JSON object")
        if args.real_runs < 1 or not 1 <= args.real_items_per_run <= 50:
            parser.error(
                "--real-runs must be positive and --real-items-per-run must be between 1 and 50"
            )
        return args
    if args.tasks < 1:
        parser.error("--tasks must be positive")
    if args.work_seconds < 0:
        parser.error("--work-seconds must be nonnegative")
    if args.fail_every < 0:
        parser.error("--fail-every must be nonnegative")
    return args


def preflight_real_profile(db: Any, template_id: str, profile_name: str, user_id: str) -> Any:
    from app.models import ModelProfile, ModelRoute, User

    profile = db.query(ModelProfile).filter(ModelProfile.name == profile_name).first()
    route = db.query(ModelRoute).filter(ModelRoute.template_id == template_id).first()
    user = db.query(User).filter(User.id == user_id).first()
    if profile is None or profile.status != "enabled":
        raise ValueError(f"Configured model profile is missing or disabled: {profile_name}")
    if route is None or route.generation_profile != profile_name:
        raise ValueError("Template generation route must match --real-profile exactly")
    if user is None or user.role != "admin":
        raise ValueError("--real-user-id must identify an existing admin user")
    return user


class RealEnqueueFailure(Exception):
    def __init__(self, elapsed_seconds: float, failure_type: str) -> None:
        super().__init__(failure_type)
        self.elapsed_seconds = elapsed_seconds
        self.failure_type = failure_type


def enqueue_real_task(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    payload: dict[str, Any],
) -> tuple[str, float]:
    started = time.monotonic()
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/api/generate",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        response_body = response.json()
        task_id = response_body.get("task_id") if isinstance(response_body, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Generation API response did not include a task_id")
        return task_id, time.monotonic() - started
    except (httpx.HTTPError, ValueError) as exc:
        raise RealEnqueueFailure(time.monotonic() - started, type(exc).__name__) from exc


def real_benchmark_configuration(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "profile": args.real_profile,
        "template_id": args.real_template_id,
        "runs": args.real_runs,
        "items_per_run": args.real_items_per_run,
        "timeout_seconds_per_run": args.timeout_seconds,
        "profile_preflight": "database route exact match",
        "api_base_url": args.real_api_base_url.rstrip("/"),
        "api_enqueue_measurement": "authenticated HTTP POST /api/generate round-trip",
    }


def real_enqueue_failure_sample(exc: RealEnqueueFailure, item_count: int) -> dict[str, Any]:
    return {
        "status": "enqueue_failed",
        "failure_type": exc.failure_type,
        "submitted_items": item_count,
        "successful_items": 0,
        "failed_items": 0,
        "cancelled_items": 0,
        # No task ID was observed; a lost response may still have accepted the request.
        "unresolved_items": item_count,
        "cost_covered_items": 0,
        "cost_unknown_items": item_count,
        "cost_unknown_trace_count": 0,
        "api_enqueue_seconds": exc.elapsed_seconds,
        "submit_to_completion_seconds": None,
        "known_estimated_trace_cost_subtotal": None,
        "estimated_trace_cost": None,
        "cost_sample_count": 0,
        "billed_cost": None,
        "enqueue_finished_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }


def run_real_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    from app.database import SessionLocal
    from app.models import GenerationTask, GenerationTaskItem, TraceLog

    db = SessionLocal()
    try:
        preflight_real_profile(db, args.real_template_id, args.real_profile, args.real_user_id)
        run_samples: list[dict[str, Any]] = []
        total_started = time.monotonic()
        with httpx.Client(timeout=args.timeout_seconds) as client:
            for _ in range(args.real_runs):
                enqueue_finished_at = None
                try:
                    task_id, enqueue_seconds = enqueue_real_task(
                        client,
                        base_url=args.real_api_base_url,
                        token=args.real_api_token,
                        payload={
                            "template_id": args.real_template_id,
                            "params": args.real_params,
                            "quantity": args.real_items_per_run,
                        },
                    )
                    enqueue_finished_at = datetime.now(timezone.utc)
                except RealEnqueueFailure as exc:
                    # Failed authenticated API submissions remain in the item denominator.
                    run_samples.append(real_enqueue_failure_sample(exc, args.real_items_per_run))
                    continue

                deadline = time.monotonic() + args.timeout_seconds
                task = None
                while time.monotonic() < deadline:
                    db.expire_all()
                    task = db.query(GenerationTask).filter(GenerationTask.id == task_id).first()
                    if task is not None and task.status in {
                        "succeeded",
                        "partially_succeeded",
                        "failed",
                        "cancelled",
                    }:
                        break
                    time.sleep(args.poll_seconds)
                terminal = task is not None and task.status in {
                    "succeeded",
                    "partially_succeeded",
                    "failed",
                    "cancelled",
                }
                items = (
                    db.query(GenerationTaskItem).filter(GenerationTaskItem.task_id == task_id).all()
                )
                traces = (
                    db.query(
                        TraceLog.trace_id, TraceLog.cost, TraceLog.usage_reported, TraceLog.stage
                    )
                    .filter(TraceLog.task_id == task_id)
                    .all()
                )
                # record_lifecycle_event emits these nonbillable stages. Keep all other
                # stages, including optional RAG provider calls, in cost coverage.
                costs = [
                    (trace_id, cost, usage_reported)
                    for trace_id, cost, usage_reported, stage in traces
                    if stage not in {"queue_dispatch", "queue_item", "workflow", "rag"}
                ]
                cost_values = [
                    float(cost)
                    for _, cost, usage_reported in costs
                    if cost is not None and usage_reported is True
                ]
                costs_by_thread: dict[str, list[float | None]] = {}
                for trace_id, cost, usage_reported in costs:
                    costs_by_thread.setdefault(trace_id, []).append(
                        cost if usage_reported is True else None
                    )
                covered_items = sum(
                    item.status in {"succeeded", "failed", "cancelled"}
                    and bool(costs_by_thread.get(item.thread_id))
                    and all(cost is not None for cost in costs_by_thread[item.thread_id])
                    for item in items
                )
                unknown_traces = sum(
                    cost is None or usage_reported is not True for _, cost, usage_reported in costs
                )
                cost_complete = covered_items == args.real_items_per_run and not unknown_traces
                known_subtotal = sum(cost_values) if cost_values else None
                successful_items = sum(item.status == "succeeded" for item in items)
                failed_items = sum(item.status == "failed" for item in items)
                cancelled_items = sum(item.status == "cancelled" for item in items)
                completed_at = task.updated_at if terminal else None
                completion_seconds = (
                    (completed_at - task.created_at).total_seconds()
                    if completed_at and task.created_at
                    else None
                )
                run_samples.append(
                    {
                        "task_id": task_id,
                        "status": (
                            task.status if terminal else "timed_out" if task else "missing_task"
                        ),
                        "submitted_items": args.real_items_per_run,
                        "successful_items": successful_items,
                        "failed_items": failed_items,
                        "cancelled_items": cancelled_items,
                        "unresolved_items": (
                            args.real_items_per_run
                            - successful_items
                            - failed_items
                            - cancelled_items
                        ),
                        "missing_items": args.real_items_per_run - len(items),
                        "api_enqueue_seconds": enqueue_seconds,
                        "submit_to_completion_seconds": completion_seconds,
                        "known_estimated_trace_cost_subtotal": known_subtotal,
                        "estimated_trace_cost": known_subtotal if cost_complete else None,
                        "cost_sample_count": len(cost_values),
                        "cost_unknown_trace_count": unknown_traces,
                        "cost_covered_items": covered_items,
                        "cost_unknown_items": args.real_items_per_run - covered_items,
                        "billed_cost": None,
                        "enqueue_finished_at": enqueue_finished_at.isoformat(),
                        "completed_at": completed_at.isoformat() if completed_at else None,
                    }
                )
        elapsed = time.monotonic() - total_started
        return {
            "benchmark_kind": "real_model_end_to_end",
            "provider_calls": "opt_in_only",
            "configuration": real_benchmark_configuration(args),
            "metrics": summarize_real_samples(run_samples),
            "elapsed_seconds": elapsed,
            "samples": run_samples,
        }
    finally:
        db.close()


def _p95(values: list[float]) -> float | None:
    from math import ceil

    if not values:
        return None
    return sorted(values)[max(0, ceil(0.95 * len(values)) - 1)]


def summarize_real_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    enqueue_values = [
        sample["api_enqueue_seconds"] for sample in samples if "api_enqueue_seconds" in sample
    ]
    completion_values = [
        sample["submit_to_completion_seconds"]
        for sample in samples
        if sample.get("submit_to_completion_seconds") is not None
    ]
    submitted = sum(sample.get("submitted_items", 0) for sample in samples)
    succeeded = sum(sample.get("successful_items", 0) for sample in samples)
    failed = sum(sample.get("failed_items", 0) for sample in samples)
    cancelled = sum(sample.get("cancelled_items", 0) for sample in samples)
    unresolved = submitted - succeeded - failed - cancelled
    estimated_costs = [
        sample["known_estimated_trace_cost_subtotal"]
        for sample in samples
        if sample.get("known_estimated_trace_cost_subtotal") is not None
    ]
    cost_covered_items = sum(sample.get("cost_covered_items", 0) for sample in samples)
    unknown_traces = sum(sample.get("cost_unknown_trace_count", 0) for sample in samples)
    cost_complete = bool(submitted) and cost_covered_items == submitted and not unknown_traces
    known_subtotal = sum(estimated_costs) if estimated_costs else None
    cost_covered_runs = sum(sample.get("estimated_trace_cost") is not None for sample in samples)
    enqueue_errors = sum(sample.get("status") == "enqueue_failed" for sample in samples)
    return {
        "sample_count": submitted,
        "submitted_items": submitted,
        "run_count": len(samples),
        "enqueue_error_run_count": enqueue_errors,
        "enqueue_error_rate": enqueue_errors / len(samples) if samples else None,
        "enqueue_error_rate_denominator": len(samples),
        "successful_items": succeeded,
        "failed_items": failed,
        "cancelled_items": cancelled,
        "unresolved_items": unresolved,
        "missing_items": sum(sample.get("missing_items", 0) for sample in samples),
        "failure_rate": failed / submitted if submitted else None,
        "failure_rate_denominator": submitted,
        "unresolved_rate": unresolved / submitted if submitted else None,
        "unresolved_rate_denominator": submitted,
        "api_enqueue_sample_count": len(enqueue_values),
        "api_enqueue_p95_seconds": _p95(enqueue_values),
        "submit_to_completion_sample_count": len(completion_values),
        "submit_to_completion_p95_seconds": _p95(completion_values),
        "estimated_trace_cost_sample_count": sum(
            sample.get("cost_sample_count", 0) for sample in samples
        ),
        "estimated_trace_cost_unknown_sample_count": unknown_traces,
        "cost_covered_items": cost_covered_items,
        "cost_unknown_items": submitted - cost_covered_items,
        "cost_coverage_denominator": submitted,
        "cost_coverage_complete": cost_complete,
        "cost_covered_runs": cost_covered_runs,
        "cost_unknown_runs": len(samples) - cost_covered_runs,
        "cost_run_coverage_denominator": len(samples),
        "known_estimated_trace_cost_subtotal": known_subtotal,
        "estimated_trace_cost_total": known_subtotal if cost_complete else None,
        "estimated_per_item_cost": (
            known_subtotal / submitted if cost_complete and known_subtotal is not None else None
        ),
        "billed_cost": None,
        "cost_basis": (
            "TraceLog.cost estimates with usage_reported=True; "
            "excludes nonbillable queue_dispatch/queue_item/workflow/rag lifecycle stages; "
            "coverage requires every remaining trace of every terminal item; "
            "billed costs unavailable"
        ),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    for index in range(args.tasks):
        submitted = datetime.now(timezone.utc)
        result = celery_app.send_task(
            "app.worker.benchmark_task.infrastructure_probe",
            args=[
                submitted.isoformat(),
                args.work_seconds,
                bool(args.fail_every and (index + 1) % args.fail_every == 0),
            ],
            queue="celery",
            task_id=str(uuid4()),
        )
        records[result.id] = {"result": result, "submitted_at": submitted.isoformat()}

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        pending = [record for record in records.values() if not record.get("sample")]
        if not pending:
            break
        for record in pending:
            result = record["result"]
            if result.ready():
                try:
                    value = result.get(timeout=0)
                    if isinstance(value, dict):
                        record["sample"] = value
                    else:
                        record["sample"] = {"status": "failed"}
                except Exception as exc:  # noqa: BLE001 - retain broker failures in denominator
                    record["sample"] = {"status": "failed", "error": type(exc).__name__}
        if any(not record.get("sample") for record in records.values()):
            time.sleep(args.poll_seconds)

    samples = []
    for record in records.values():
        sample = record.get("sample")
        if sample is None:
            sample = {"status": "timed_out"}
        samples.append(sample)
    elapsed = time.monotonic() - started
    return {
        "benchmark_kind": "celery_redis_worker_infrastructure_stub",
        "provider_calls": 0,
        "configuration": {
            "tasks": args.tasks,
            "worker_concurrency": args.worker_concurrency,
            "work_seconds": args.work_seconds,
            "fail_every": args.fail_every,
            "timeout_seconds": args.timeout_seconds,
            "broker_backend": "Redis (URL omitted)",
            "queue": "celery",
        },
        "elapsed_seconds": elapsed,
        "metrics": summarize_samples(samples, elapsed),
        "samples": samples,
    }


def main() -> int:
    args = parse_args()
    report = run_real_benchmark(args) if args.arm == "real" else run_benchmark(args)
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    print(f"Redis configured: {'yes' if settings.REDIS_URL else 'no'}", file=sys.stderr)
    failed = report["metrics"].get("failed", report["metrics"].get("failed_items", 0))
    incomplete = report["metrics"].get("unresolved_items", 0) + report["metrics"].get(
        "cancelled_items", 0
    )
    return 0 if failed == 0 and incomplete == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
