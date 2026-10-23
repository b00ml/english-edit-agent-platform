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
from uuid import uuid4

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
            )
            if not value
        ]
        if missing:
            parser.error(f"--arm real requires configured values: {', '.join(missing)}")
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


def run_real_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    from app.database import SessionLocal
    from app.models import GenerationTask, GenerationTaskItem, TraceLog
    from app.schemas import GenerateRequest
    from app.services.generation_service import GenerationService

    db = SessionLocal()
    try:
        user = preflight_real_profile(
            db, args.real_template_id, args.real_profile, args.real_user_id
        )
        run_samples: list[dict[str, Any]] = []
        total_started = time.monotonic()
        for _ in range(args.real_runs):
            enqueue_started = time.monotonic()
            response = GenerationService(db).create_task(
                GenerateRequest(
                    template_id=args.real_template_id,
                    params=args.real_params,
                    quantity=args.real_items_per_run,
                ),
                user,
                celery_app,
            )
            enqueue_seconds = time.monotonic() - enqueue_started
            enqueue_finished_at = datetime.now(timezone.utc)
            deadline = time.monotonic() + args.timeout_seconds
            task = None
            while time.monotonic() < deadline:
                db.expire_all()
                task = (
                    db.query(GenerationTask).filter(GenerationTask.id == response.task_id).first()
                )
                if task is not None and task.status in {
                    "succeeded",
                    "partially_succeeded",
                    "failed",
                    "cancelled",
                }:
                    break
                time.sleep(args.poll_seconds)
            if task is None or task.status not in {
                "succeeded",
                "partially_succeeded",
                "failed",
                "cancelled",
            }:
                run_samples.append(
                    {
                        "status": "timed_out" if task is not None else "missing_task",
                        "task_id": task.id if task is not None else response.task_id,
                        "submitted_items": args.real_items_per_run,
                        "successful_items": 0,
                        "failed_items": args.real_items_per_run,
                        "api_enqueue_seconds": enqueue_seconds,
                        "submit_to_completion_seconds": None,
                        "estimated_trace_cost": None,
                        "cost_sample_count": 0,
                        "billed_cost": None,
                        "enqueue_finished_at": enqueue_finished_at.isoformat(),
                        "completed_at": None,
                    }
                )
                break
            items = db.query(GenerationTaskItem).filter(GenerationTaskItem.task_id == task.id).all()
            costs = db.query(TraceLog.cost).filter(TraceLog.task_id == task.id).all()
            cost_values = [float(row[0]) for row in costs if row[0] is not None]
            estimated_cost = sum(cost_values) if cost_values else None
            successful_items = sum(item.status == "succeeded" for item in items)
            completed_at = task.updated_at
            completion_seconds = (
                (completed_at - task.created_at).total_seconds()
                if completed_at and task.created_at
                else None
            )
            run_samples.append(
                {
                    "task_id": task.id,
                    "status": task.status,
                    "submitted_items": args.real_items_per_run,
                    "successful_items": successful_items,
                    "failed_items": args.real_items_per_run - successful_items,
                    "api_enqueue_seconds": enqueue_seconds,
                    "submit_to_completion_seconds": completion_seconds,
                    "estimated_trace_cost": estimated_cost,
                    "cost_sample_count": len(cost_values),
                    "billed_cost": None,
                    "enqueue_finished_at": enqueue_finished_at.isoformat(),
                    "completed_at": completed_at.isoformat() if completed_at else None,
                }
            )
        elapsed = time.monotonic() - total_started
        return {
            "benchmark_kind": "real_model_end_to_end",
            "provider_calls": "opt_in_only",
            "configuration": {
                "profile": args.real_profile,
                "template_id": args.real_template_id,
                "runs": args.real_runs,
                "items_per_run": args.real_items_per_run,
                "timeout_seconds_per_run": args.timeout_seconds,
                "profile_preflight": "database route exact match",
                "api_enqueue_measurement": "GenerationService.create_task duration; excludes HTTP transport",
            },
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
    failed = submitted - succeeded
    estimated_costs = [
        sample["estimated_trace_cost"]
        for sample in samples
        if sample.get("estimated_trace_cost") is not None
    ]
    cost_covered_items = sum(
        sample.get("submitted_items", 0)
        for sample in samples
        if sample.get("estimated_trace_cost") is not None
    )
    return {
        "sample_count": submitted,
        "run_count": len(samples),
        "failed_items": failed,
        "failure_rate": failed / submitted if submitted else None,
        "api_enqueue_sample_count": len(enqueue_values),
        "api_enqueue_p95_seconds": _p95(enqueue_values),
        "submit_to_completion_sample_count": len(completion_values),
        "submit_to_completion_p95_seconds": _p95(completion_values),
        "estimated_trace_cost_sample_count": sum(
            sample.get("cost_sample_count", 0) for sample in samples
        ),
        "estimated_per_item_cost": (
            sum(estimated_costs) / cost_covered_items if cost_covered_items else None
        ),
        "billed_cost": None,
        "cost_basis": "TraceLog.cost estimated values; billed costs unavailable",
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
                except Exception as exc:  # broker task failure remains in submitted denominator
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
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
