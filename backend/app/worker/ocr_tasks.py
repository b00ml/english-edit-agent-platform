"""OCR uses its own single-concurrency queue, not the generation worker slots."""

from typing import Any

from celery import Task, shared_task
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.worker.ocr_runner import run_job, sweep


# Celery exposes an untyped registration decorator; the function body remains typed.
@shared_task(  # type: ignore[untyped-decorator]
    name="app.worker.ocr_tasks.process_ocr_job",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    max_retries=settings.TASK_MAX_RETRIES,
    soft_time_limit=int(settings.RAG_OCR_PAGE_TIMEOUT + 60),
    time_limit=int(settings.RAG_OCR_PAGE_TIMEOUT + 90),
)
def process_ocr_job(self: Task, job_id: str) -> dict[str, Any]:
    return run_job(job_id)


@shared_task(  # type: ignore[untyped-decorator]
    name="app.worker.ocr_tasks.sweep_ocr_jobs", acks_late=True
)
def sweep_ocr_jobs() -> dict[str, int]:
    result = sweep()
    from app.worker.boundary import sweep_boundaries

    result.update(sweep_boundaries())
    return result


@shared_task(  # type: ignore[untyped-decorator]
    name="app.worker.ocr_tasks.index_ocr_job",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.RAG_OCR_INDEX_TIMEOUT,
    time_limit=settings.RAG_OCR_INDEX_TIMEOUT + 30,
)
def index_ocr_job(job_id: str) -> dict[str, Any]:
    from app.worker.ocr_index import run_index

    return run_index(job_id)


@shared_task(  # type: ignore[untyped-decorator]
    name="app.worker.ocr_tasks.review_boundary",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.RAG_OCR_BOUNDARY_TIMEOUT + 60,
    time_limit=settings.RAG_OCR_BOUNDARY_TIMEOUT + 90,
)
def review_boundary(identifier: str) -> dict[str, Any]:
    from app.worker.boundary import run_boundary

    return run_boundary(identifier)
