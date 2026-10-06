"""重放指定 task_outbox 死信事件。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.outbox import relay_pending, replay_dead  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402


def main() -> int:
    """显式恢复指定死信并尝试投递；失败保留待周期重试，不代替下游执行验收。"""
    parser = argparse.ArgumentParser(description="Replay a dead outbox event")
    parser.add_argument("event_id", help="task_outbox.id (row UUID, not logical event_id)")
    args = parser.parse_args()
    session = SessionLocal()
    try:
        event = replay_dead(session, args.event_id)
        result = relay_pending(
            session,
            lambda name, args, task_id=None: celery_app.send_task(name, args=args, task_id=task_id),
            only_event_id=event.id,
            limit=1,
        )
        session.refresh(event)
        print(f"replayed event_id={event.id} status={event.status} delivery={result}")
        return 0
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
