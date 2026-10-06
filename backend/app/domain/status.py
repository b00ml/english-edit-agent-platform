# app/domain/status.py —— 状态契约（优化技术设计 3.0 / P0-1）
# 集中定义任务、任务条目与内容状态，禁止在 API / workflow / worker 中散落魔法值。
from typing import Final

# ---------------------------------------------------------------------------
# 生成任务
# ---------------------------------------------------------------------------
TASK_PENDING: Final = "pending"
TASK_DISPATCHED: Final = "dispatched"
TASK_RUNNING: Final = "running"
TASK_AWAITING_REVIEW: Final = "awaiting_review"
TASK_SUCCEEDED: Final = "succeeded"
TASK_PARTIALLY_SUCCEEDED: Final = "partially_succeeded"
TASK_FAILED: Final = "failed"
TASK_CANCELLED: Final = "cancelled"

ACTIVE_TASK_STATUSES: Final[tuple[str, ...]] = (
    TASK_PENDING,
    TASK_DISPATCHED,
    TASK_RUNNING,
    TASK_AWAITING_REVIEW,
)

TERMINAL_TASK_STATUSES: Final[tuple[str, ...]] = (
    TASK_SUCCEEDED,
    TASK_PARTIALLY_SUCCEEDED,
    TASK_FAILED,
    TASK_CANCELLED,
)

# ---------------------------------------------------------------------------
# 生成任务条目（generation_task_item）
# ---------------------------------------------------------------------------
ITEM_PENDING: Final = "pending"
ITEM_RUNNING: Final = "running"
ITEM_SUCCEEDED: Final = "succeeded"
ITEM_AWAITING_REVIEW: Final = "awaiting_review"
ITEM_FAILED: Final = "failed"
ITEM_CANCELLED: Final = "cancelled"
# Compatibility symbol names, not alternative values persisted to SQL.
ITEM_QUEUED: Final = ITEM_PENDING
ITEM_STORED: Final = ITEM_SUCCEEDED
ITEM_REJECTED: Final = ITEM_FAILED
ITEM_STATUSES: Final = (
    ITEM_PENDING,
    ITEM_RUNNING,
    ITEM_SUCCEEDED,
    ITEM_AWAITING_REVIEW,
    ITEM_FAILED,
    ITEM_CANCELLED,
)
TERMINAL_ITEM_STATUSES: Final = (ITEM_SUCCEEDED, ITEM_FAILED, ITEM_CANCELLED)
DELIVERY_PROTECTED_ITEM_STATUSES: Final = (*TERMINAL_ITEM_STATUSES, ITEM_AWAITING_REVIEW)
DELIVERY_TASK_STATUSES: Final = (TASK_PENDING, TASK_DISPATCHED, TASK_RUNNING)
TASK_STATUSES: Final = (*ACTIVE_TASK_STATUSES, *TERMINAL_TASK_STATUSES)


def item_status_from_graph(status: str, interrupted: bool = False) -> str:
    from app.errors import InvalidGenerationStateError

    if interrupted:
        return ITEM_AWAITING_REVIEW
    mapping = {
        "stored": ITEM_SUCCEEDED,
        "review_passed": ITEM_SUCCEEDED,
        "rejected": ITEM_FAILED,
        "review_rejected": ITEM_FAILED,
        "awaiting_review": ITEM_AWAITING_REVIEW,
        "cancelled": ITEM_CANCELLED,
        "failed": ITEM_FAILED,
    }
    if status not in mapping:
        raise InvalidGenerationStateError(f"未知Graph结果状态: {status}")
    return mapping[status]


def item_status_from_content(status: str) -> str:
    from app.errors import InvalidGenerationStateError

    mapping = {
        "pending_qc": ITEM_SUCCEEDED,
        "passed": ITEM_SUCCEEDED,
        "published": ITEM_SUCCEEDED,
        "rejected": ITEM_FAILED,
        "awaiting_review": ITEM_AWAITING_REVIEW,
    }
    if status not in mapping:
        raise InvalidGenerationStateError(f"未知内容状态: {status}")
    return mapping[status]


def aggregate_item_statuses(statuses: list[str], expected: int) -> tuple[str | None, float]:
    """Review waiting is active, not terminal success; cancellation is distinct."""
    from app.errors import InvalidGenerationStateError

    if expected < 1 or any(s not in ITEM_STATUSES for s in statuses):
        raise InvalidGenerationStateError("任务数量或子任务状态不合法")
    total = max(expected, len(statuses))
    resolved = sum(s in TERMINAL_ITEM_STATUSES for s in statuses)
    progress = round(resolved / total, 4)
    if resolved < total:
        if len(statuses) == total and all(s in DELIVERY_PROTECTED_ITEM_STATUSES for s in statuses):
            return TASK_AWAITING_REVIEW, progress
        return None, progress
    if all(s == ITEM_SUCCEEDED for s in statuses):
        return TASK_SUCCEEDED, progress
    if all(s == ITEM_CANCELLED for s in statuses):
        return TASK_CANCELLED, progress
    return (TASK_PARTIALLY_SUCCEEDED if ITEM_SUCCEEDED in statuses else TASK_FAILED), progress


# ---------------------------------------------------------------------------
# 内容条目
# ---------------------------------------------------------------------------
CONTENT_PENDING_QC: Final = "pending_qc"
CONTENT_PASSED: Final = "passed"
CONTENT_REJECTED: Final = "rejected"
CONTENT_PUBLISHED: Final = "published"
CONTENT_AWAITING_REVIEW: Final = "awaiting_review"

CONTENT_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    CONTENT_PENDING_QC: (CONTENT_PASSED, CONTENT_REJECTED, CONTENT_AWAITING_REVIEW),
    CONTENT_AWAITING_REVIEW: (CONTENT_PASSED, CONTENT_REJECTED),
    CONTENT_PASSED: (CONTENT_PUBLISHED,),
    CONTENT_PUBLISHED: (),
    CONTENT_REJECTED: (),
}


def can_transition_content(current: str, target: str) -> bool:
    """判断内容状态是否允许从 current 迁移到 target。"""
    return target in CONTENT_TRANSITIONS.get(current, ())
