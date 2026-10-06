import sys

sys.path.insert(0, "/code")
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import GenerationTask

db = SessionLocal()
print("--- non-terminal tasks ---")
for t in (
    db.query(GenerationTask)
    .filter(GenerationTask.status.in_(["pending", "running"]))
    .order_by(GenerationTask.created_at.desc())
    .all()
):
    age = (datetime.now(timezone.utc) - t.created_at).total_seconds()
    print(
        f"id={t.id} status={t.status} created={t.created_at.isoformat()} age={age:.0f}s tpl={t.template_id}"
    )
db.close()
