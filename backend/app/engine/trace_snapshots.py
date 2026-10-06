"""Bounded encrypted snapshots, transactional quota, redaction and expiring access."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import TraceSnapshotError
from app.models import TraceLog, TraceSnapshot


@dataclass(frozen=True)
class PreparedSnapshot:
    status: str
    ciphertext: str = ""
    content_hash: str = ""
    replay_level: str = ""


def _cipher() -> Fernet:
    try:
        return Fernet(settings.TRACE_SNAPSHOT_SECRET_KEY.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise TraceSnapshotError("快照加密密钥未配置或无效") from exc


def prepare_snapshot(data: dict[str, Any] | None) -> PreparedSnapshot:
    # Any limited to variable JSON request/response envelope, never SDK objects/headers.
    if not settings.TRACE_SNAPSHOT_ENABLED:
        return PreparedSnapshot("disabled")
    if data is None:
        return PreparedSnapshot("not_requested")
    from app.engine.trace import sanitize_trace_data

    sanitized = sanitize_trace_data(data, max_string_length=settings.TRACE_SNAPSHOT_MAX_BYTES)
    try:
        encoded = json.dumps(
            sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        return PreparedSnapshot("serialization_error")
    if len(encoded) > settings.TRACE_SNAPSHOT_MAX_BYTES:
        return PreparedSnapshot("oversize")
    try:
        cipher = _cipher()
    except TraceSnapshotError:
        return PreparedSnapshot("key_unavailable")
    return PreparedSnapshot(
        "available",
        cipher.encrypt(encoded).decode("ascii"),
        hashlib.sha256(encoded).hexdigest(),
        (
            "redacted_request_response"
            if sanitized.get("response_text") is not None
            else "request_only"
        ),
    )


def _quota_lock(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 77120261006})


def expire_snapshots(session: Session, now: datetime | None = None) -> int:
    instant = now or datetime.now(timezone.utc)
    ids = list(
        session.scalars(
            select(TraceSnapshot.trace_row_id)
            .where(TraceSnapshot.expires_at <= instant)
            .order_by(TraceSnapshot.expires_at)
            .limit(200)
        )
    )
    if ids:
        session.query(TraceLog).filter(TraceLog.id.in_(ids)).update(
            {TraceLog.snapshot_status: "expired"}, synchronize_session=False
        )
        session.query(TraceSnapshot).filter(TraceSnapshot.trace_row_id.in_(ids)).delete(
            synchronize_session=False
        )
    return len(ids)


def store_snapshot(session: Session, trace: TraceLog, prepared: PreparedSnapshot) -> None:
    trace.snapshot_status = prepared.status
    if prepared.status != "available":
        return
    _quota_lock(session)
    expire_snapshots(session)
    used = int(session.scalar(select(func.coalesce(func.sum(TraceSnapshot.stored_bytes), 0))) or 0)
    size = len(prepared.ciphertext.encode("ascii"))
    if used + size > settings.TRACE_SNAPSHOT_TOTAL_BYTES:
        trace.snapshot_status = "budget_exceeded"
        return
    session.add(
        TraceSnapshot(
            trace_row_id=trace.id,
            ciphertext=prepared.ciphertext,
            stored_bytes=size,
            content_hash=prepared.content_hash,
            replay_level=prepared.replay_level,
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.TRACE_SNAPSHOT_RETENTION_DAYS),
        )
    )


def read_snapshot(session: Session, row_id: str) -> tuple[TraceSnapshot, dict[str, Any]]:
    snapshot = session.get(TraceSnapshot, row_id)
    if snapshot is None:
        raise TraceSnapshotError("该调用没有可用快照：可能是旧记录、超限、未开启或已清理")
    expires = snapshot.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise TraceSnapshotError("快照已过保留期")
    try:
        decoded = _cipher().decrypt(snapshot.ciphertext.encode("ascii"))
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise TraceSnapshotError("快照解密失败；需恢复原独立加密密钥") from exc
    if hashlib.sha256(decoded).hexdigest() != snapshot.content_hash:
        raise TraceSnapshotError("快照完整性校验失败")
    try:
        data = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise TraceSnapshotError("快照内容协议无效") from exc
    if not isinstance(data, dict):
        raise TraceSnapshotError("快照协议无效")
    return snapshot, data
