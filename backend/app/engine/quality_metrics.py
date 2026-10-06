"""Automatic quality metrics over per-draft evaluations, not retained content only."""

from collections import defaultdict
from collections.abc import Iterable
from typing import TypedDict

from app.models import QualityEvaluation


class QualityPipelineStats(TypedDict):
    threads: int
    evaluations: int
    first_evaluated_threads: int
    finalized_threads: int
    first_pass_rate: float | None
    final_pass_rate: float | None
    quality_failure_rate: float | None


def quality_pipeline_stats(events: Iterable[QualityEvaluation]) -> QualityPipelineStats:
    groups: dict[str, list[QualityEvaluation]] = defaultdict(list)
    for event in events:
        groups[event.thread_id].append(event)
    ordered = [sorted(rows, key=lambda event: event.revise_count) for rows in groups.values()]
    first = [rows[0] for rows in ordered if rows[0].revise_count == 0]
    final = [rows[-1] for rows in ordered if rows[-1].is_final]

    def passed(event: QualityEvaluation) -> bool:
        return (
            event.status == "completed"
            and event.score is not None
            and event.score >= event.threshold
        )

    return {
        "threads": len(ordered),
        "evaluations": sum(len(rows) for rows in ordered),
        "first_evaluated_threads": len(first),
        "finalized_threads": len(final),
        "first_pass_rate": sum(passed(event) for event in first) / len(first) if first else None,
        "final_pass_rate": sum(passed(event) for event in final) / len(final) if final else None,
        "quality_failure_rate": (
            sum(event.status == "failed" for event in final) / len(final) if final else None
        ),
    }
