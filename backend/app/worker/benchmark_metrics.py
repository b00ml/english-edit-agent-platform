"""Summary helpers for the infrastructure-only worker benchmark."""

from math import ceil
from typing import Any


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def summarize_samples(samples: list[dict[str, Any]], elapsed_seconds: float) -> dict[str, Any]:
    submitted = len(samples)
    succeeded = sum(sample["status"] == "succeeded" for sample in samples)
    failed = submitted - succeeded
    queue_latencies = [
        float(sample["queue_latency_seconds"])
        for sample in samples
        if sample.get("queue_latency_seconds") is not None
    ]
    end_to_end = [
        float(sample["end_to_end_seconds"])
        for sample in samples
        if sample.get("end_to_end_seconds") is not None
    ]
    rss = [
        int(sample["worker_rss_bytes"])
        for sample in samples
        if sample.get("worker_rss_bytes") is not None
    ]
    cpu = [
        float(sample["worker_cpu_seconds"])
        for sample in samples
        if sample.get("worker_cpu_seconds") is not None
    ]
    return {
        "submitted": submitted,
        "succeeded": succeeded,
        "failed": failed,
        "failure_rate": failed / submitted if submitted else None,
        "successful_throughput_per_hour": (
            succeeded * 3600 / elapsed_seconds if submitted and elapsed_seconds > 0 else None
        ),
        "queue_latency_sample_count": len(queue_latencies),
        "queue_latency_p95_seconds": _percentile(queue_latencies, 0.95),
        "end_to_end_sample_count": len(end_to_end),
        "end_to_end_p95_seconds": _percentile(end_to_end, 0.95),
        "worker_rss_sample_count": len(rss),
        "worker_rss_max_bytes": max(rss) if rss else None,
        "worker_cpu_sample_count": len(cpu),
        "worker_cpu_seconds_total": sum(cpu) if cpu else None,
    }
