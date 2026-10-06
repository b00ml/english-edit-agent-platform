"""
内容 Repository
职责：ContentItem 数据访问、查询
"""

from typing import List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.models import ContentItem
from app.repositories.base import BaseRepository


class ContentRepository(BaseRepository[ContentItem]):
    """内容数据访问层。"""

    def __init__(self, db: Session):
        super().__init__(ContentItem, db)

    def list_by_task(self, task_id: str, skip: int = 0, limit: int = 100) -> List[ContentItem]:
        """查询任务的内容列表。"""
        return (
            self.db.query(ContentItem)
            .filter(ContentItem.task_id == task_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_task(self, task_id: str) -> int:
        """Count all content rows for the requested task, matching Service contract."""
        return int(
            self.db.query(func.count(ContentItem.id))
            .filter(ContentItem.task_id == task_id)
            .scalar()
            or 0
        )

    def list_by_status(self, status: str, skip: int = 0, limit: int = 100) -> List[ContentItem]:
        """按状态查询内容。"""
        return (
            self.db.query(ContentItem)
            .filter(ContentItem.status == status)
            .order_by(desc(ContentItem.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_by_tenant(self, tenant_id: int, skip: int = 0, limit: int = 100) -> List[ContentItem]:
        """查询租户的内容列表。"""
        return (
            self.db.query(ContentItem)
            .filter(ContentItem.tenant_id == tenant_id)
            .order_by(desc(ContentItem.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_published_by_tenant(
        self, tenant_id: int, skip: int = 0, limit: int = 100
    ) -> List[ContentItem]:
        """查询租户已发布的内容（viewer 可见）。"""
        return (
            self.db.query(ContentItem)
            .filter(ContentItem.tenant_id == tenant_id)
            .filter(ContentItem.status == "published")
            .order_by(desc(ContentItem.published_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_status(self, status: str, tenant_id: str | None = None) -> int:
        """统计指定状态的内容数。"""
        query = self.db.query(func.count(ContentItem.id)).filter(ContentItem.status == status)
        if tenant_id is not None:
            query = query.filter(ContentItem.tenant_id == tenant_id)
        return int(query.scalar() or 0)

    def count_by_tenant(self, tenant_id: int) -> int:
        """统计租户内容数。"""
        return (
            self.db.query(func.count(ContentItem.id))
            .filter(ContentItem.tenant_id == tenant_id)
            .scalar()
        )

    def count_by_tenant_and_status(self, tenant_id: int, status: str) -> int:
        """统计租户指定状态的内容数。"""
        return (
            self.db.query(func.count(ContentItem.id))
            .filter(ContentItem.tenant_id == tenant_id)
            .filter(ContentItem.status == status)
            .scalar()
        )

    def list_by_template(
        self, template_id: str, skip: int = 0, limit: int = 100
    ) -> List[ContentItem]:
        """按模板查询内容。"""
        return (
            self.db.query(ContentItem)
            .filter(ContentItem.template_id == template_id)
            .order_by(desc(ContentItem.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_status_summary(self, tenant_id: str) -> dict:
        """
        按状态统计内容数量

        返回: {"draft": 50, "passed": 80, "published": 60, "rejected": 10}
        """
        from sqlalchemy import case

        result = (
            self.db.query(
                func.count(case((self.model.status == "draft", 1))).label("draft"),
                func.count(case((self.model.status == "passed", 1))).label("passed"),
                func.count(case((self.model.status == "published", 1))).label("published"),
                func.count(case((self.model.status == "rejected", 1))).label("rejected"),
            )
            .filter(self.model.tenant_id == tenant_id)
            .first()
        )

        return {
            "draft": int(result.draft or 0),
            "passed": int(result.passed or 0),
            "published": int(result.published or 0),
            "rejected": int(result.rejected or 0),
        }
