"""运行时健康检查与依赖探针。"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.database import SessionLocal


def _probe(name: str, check: Any) -> dict[str, Any]:
    started = time.perf_counter_ns()
    try:
        detail = check()
        return {
            "name": name,
            "status": "healthy",
            "latency_ms": _elapsed(started),
            "detail": detail,
        }
    except Exception as exc:  # noqa: BLE001 - probe must report failure, never break health API
        return {
            "name": name,
            "status": "unavailable",
            "latency_ms": _elapsed(started),
            "detail": type(exc).__name__,
        }


def _elapsed(started: int) -> float:
    return round((time.perf_counter_ns() - started) / 1_000_000, 2)


def check_database() -> dict[str, Any]:
    def _check() -> str:
        session = SessionLocal()
        try:
            session.execute(text("SELECT 1"))
            return "ok"
        finally:
            session.close()

    return _probe("database", _check)


def check_alembic() -> dict[str, Any]:
    def _check() -> str:
        session = SessionLocal()
        try:
            from app.main import _verify_migrations

            return _verify_migrations(session)
        finally:
            session.close()

    return _probe("alembic", _check)


def check_redis() -> dict[str, Any]:
    def _check() -> str:
        import redis

        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        try:
            client.ping()
            return "ok"
        finally:
            client.close()

    return _probe("redis", _check)


def check_checkpointer() -> dict[str, Any]:
    started = time.perf_counter_ns()
    backend = settings.CHECKPOINTER_BACKEND
    if backend == "memory":
        status = "degraded"
        detail = "MemorySaver（仅进程内）"
    else:
        try:
            from langgraph.checkpoint.memory import MemorySaver

            from app.workflow.graph import _get_checkpointer

            saver = _get_checkpointer()
            if isinstance(saver, MemorySaver):
                status = "degraded"
                detail = "PostgresSaver 不可用，已降级 MemorySaver"
            else:
                status = "healthy"
                detail = "PostgresSaver"
        except Exception as exc:  # noqa: BLE001 - probe converts initialization failures to status
            status = "unavailable"
            detail = str(exc)[:200]
    return {
        "name": "checkpointer",
        "status": status,
        "latency_ms": _elapsed(started),
        "detail": detail,
    }


def dependency_report() -> dict[str, Any]:
    from app.engine.trace_recovery import trace_diagnostics

    diagnostics = trace_diagnostics()
    checks = [
        check_database(),
        check_alembic(),
        check_redis(),
        check_checkpointer(),
        {
            "name": "trace",
            "status": diagnostics["status"],
            "latency_ms": 0.0,
            "detail": f"process={diagnostics['process_id']} spooled={diagnostics['spooled']}",
        },
    ]
    statuses = {item["status"] for item in checks}
    overall = (
        "unavailable"
        if "unavailable" in statuses
        else "degraded" if "degraded" in statuses else "healthy"
    )
    return {"status": overall, "dependencies": checks}


def readiness_report() -> dict[str, Any]:
    report = dependency_report()
    critical = {"database", "alembic", "redis"}
    failed = [
        item
        for item in report["dependencies"]
        if item["name"] in critical and item["status"] != "healthy"
    ]
    checkpointer = next(item for item in report["dependencies"] if item["name"] == "checkpointer")
    if checkpointer["status"] == "unavailable":
        failed.append(checkpointer)
    elif (
        checkpointer["status"] == "degraded"
        and settings.ENVIRONMENT in {"staging", "production"}
        and not settings.ALLOW_MEMORY_CHECKPOINTER
    ):
        failed.append(checkpointer)
    if settings.TRACE_REQUIRE_DURABILITY:
        failed.extend(
            item
            for item in report["dependencies"]
            if item["name"] == "trace" and item["status"] == "unavailable"
        )
    report["ready"] = not failed
    report["blocking"] = [item["name"] for item in failed]
    if failed:
        report["status"] = "unavailable"
    return report
