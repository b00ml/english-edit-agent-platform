"""Celery task used only to measure broker and worker infrastructure."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from celery import shared_task


def _rss_bytes() -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _cpu_seconds() -> float | None:
    return time.process_time()


@shared_task(  # type: ignore[untyped-decorator]
    name="app.worker.benchmark_task.infrastructure_probe", ignore_result=False
)
def infrastructure_probe(
    submitted_at: str, work_seconds: float, should_fail: bool = False
) -> dict[str, object]:
    """Measure a small configurable worker delay; performs no application work."""
    started = datetime.now(timezone.utc)
    cpu_before = _cpu_seconds()
    time.sleep(work_seconds)
    cpu_after = _cpu_seconds()
    finished = datetime.now(timezone.utc)
    submitted = datetime.fromisoformat(submitted_at)
    return {
        "status": "failed" if should_fail else "succeeded",
        "submitted_at": submitted.isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "queue_latency_seconds": (started - submitted).total_seconds(),
        "end_to_end_seconds": (finished - submitted).total_seconds(),
        "worker_rss_bytes": _rss_bytes(),
        "worker_cpu_seconds": (
            cpu_after - cpu_before if cpu_before is not None and cpu_after is not None else None
        ),
        "worker_pid": os.getpid(),
    }
