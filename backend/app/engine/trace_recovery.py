"""Durable local Trace fallback and replay, with process-local health counters."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import settings
from app.errors import TracePersistenceError

_lock = threading.Lock()
_counters: dict[str, dict[str, int | str | None]] = {}
_unrecoverable = False


def note_sink(name: str, success: bool, error_type: str | None = None) -> None:
    with _lock:
        state = _counters.setdefault(name, {"attempts": 0, "failures": 0, "last_error_type": None})
        state["attempts"] = int(state["attempts"] or 0) + 1
        if not success:
            state["failures"] = int(state["failures"] or 0) + 1
        state["last_error_type"] = error_type


def note_durability(success: bool) -> None:
    global _unrecoverable
    with _lock:
        # A later successful write cannot recover an earlier irretrievably lost record.
        _unrecoverable = _unrecoverable or not success


def reset_diagnostics() -> None:
    global _unrecoverable
    with _lock:
        _counters.clear()
        _unrecoverable = False


def spool_trace(trace: dict[str, Any]) -> bool:
    """The payload is already recursively sanitized; write once using an exclusive file."""
    if not settings.TRACE_FAILURE_SPOOL_DIR:
        return False
    directory = Path(settings.TRACE_FAILURE_SPOOL_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{trace['id']}.json"
    temporary = directory / f"{trace['id']}.tmp"
    if path.exists():
        raise FileExistsError("Trace id already spooled")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(trace, ensure_ascii=False))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return True


def replay_spool(emit: Callable[[dict[str, Any]], None], limit: int = 100) -> dict[str, int]:
    if not settings.TRACE_FAILURE_SPOOL_DIR:
        return {"replayed": 0, "failed": 0}
    directory = Path(settings.TRACE_FAILURE_SPOOL_DIR)
    replayed = failed = 0
    for path in sorted(directory.glob("*.json"))[:limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            emit(payload)
            sink_name = getattr(getattr(emit, "__self__", None), "name", "replay")
            note_sink(sink_name, True)
            note_durability(True)
            path.unlink(missing_ok=True)
            replayed += 1
        except Exception as exc:  # noqa: BLE001 - failed replay must keep the durable record
            note_sink("replay", False, type(exc).__name__)
            failed += 1
    return {"replayed": replayed, "failed": failed}


def trace_diagnostics() -> dict[str, Any]:
    queued = (
        len(list(Path(settings.TRACE_FAILURE_SPOOL_DIR).glob("*.json")))
        if settings.TRACE_FAILURE_SPOOL_DIR
        else 0
    )
    with _lock:
        states = {key: dict(value) for key, value in _counters.items()}
        unavailable = _unrecoverable
    degraded = queued > 0 or any(value["last_error_type"] is not None for value in states.values())
    return {
        "status": "unavailable" if unavailable else "degraded" if degraded else "healthy",
        "process_id": os.getpid(),
        "scope": "process-local; backlog is the configured durable volume",
        "spooled": queued,
        "sinks": states,
    }


def pending_task_cost(task_id: str | None) -> float:
    if not settings.TRACE_FAILURE_SPOOL_DIR or task_id is None:
        return 0.0
    total = 0.0
    try:
        for path in Path(settings.TRACE_FAILURE_SPOOL_DIR).glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("task_id") == task_id:
                total += float(payload.get("cost") or 0.0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TracePersistenceError("补偿成本日志不可读取，无法验证任务预算") from exc
    return total
