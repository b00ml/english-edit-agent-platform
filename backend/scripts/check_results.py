import sys

sys.path.insert(0, "/code")
from sqlalchemy import func

from app.database import SessionLocal
from app.models import ContentItem, GenerationTask

db = SessionLocal()
print("--- stress.log ---")
try:
    with open("/tmp/stress.log", encoding="utf-8") as f:
        print(f.read())
except FileNotFoundError:
    print("(no log)")
print("--- task status ---")
for s, c in db.query(GenerationTask.status, func.count()).group_by(GenerationTask.status).all():
    print(f"{s}: {c}")
print("--- content items ---")
print("content_item rows:", db.query(func.count(ContentItem.id)).scalar())
db.close()
