"""Nonblocking ownership of one item across its entire worker body (including commits)."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


@contextmanager
def item_execution_lock(session: Session, thread_id: str) -> Iterator[bool]:
    # Distinct namespace from the Graph lock; nesting the same DB lock on another
    # connection would deadlock. Reconciliation uses this same item namespace.
    key = int.from_bytes(
        hashlib.sha256(("worker-item:" + thread_id).encode()).digest()[:8], "big", signed=True
    )
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        with bind.engine.connect() as connection:
            acquired = bool(
                connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
    else:
        with _guard:
            lock = _locks.setdefault(thread_id, threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
