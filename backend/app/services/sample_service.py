"""Sample pool API uses the same scoped pooling functions for reads and writes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import InvalidKnowledgeError
from app.models import ContentItem, SamplePool, User
from app.sample_pool import list_samples, pool_item, sync_eligible
from app.tenancy import require_scope, scope_query


class SampleService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_sample_from_content(
        self, content_id: str, source: str, purpose: str, user: User
    ) -> SamplePool:
        item = require_scope(self.db.get(ContentItem, content_id), user)
        if item.status not in {"passed", "published"}:
            raise InvalidKnowledgeError("仅人工通过或已发布内容可沉淀为样本")
        return pool_item(self.db, item, source=source, purpose=purpose)

    def sync_samples(self, user: User) -> dict[str, int]:
        added = sync_eligible(self.db, user=user)
        total = scope_query(self.db.query(SamplePool), SamplePool, user).count()
        return {"added": added, "total": total}

    def list_samples_filtered(
        self,
        template_id: str | None,
        knowledge_point: str | None,
        purpose: str | None,
        source: str | None,
        page: int,
        page_size: int,
        user: User,
    ) -> tuple[list[SamplePool], int]:
        return list_samples(
            self.db,
            template_id=template_id,
            knowledge_point=knowledge_point,
            purpose=purpose,
            source=source,
            page=page,
            page_size=page_size,
            user=user,
        )

    def delete_sample_by_id(self, sample_id: str, user: User) -> dict[str, str]:
        row = require_scope(self.db.get(SamplePool, sample_id), user)
        self.db.delete(row)
        self.db.commit()
        return {"deleted": sample_id}
