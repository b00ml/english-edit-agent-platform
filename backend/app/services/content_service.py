"""
内容 Service
职责：内容查询、状态管理、发布
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.domain.status import can_transition_content
from app.engine.content_validation import inspect_content
from app.errors import ContentStateConflictError
from app.models import ContentItem, QuestionTemplate, User
from app.repositories import ContentRepository
from app.schemas import ContentListOut, ContentOut
from app.services.content_guard import require_valid_content
from app.tenancy import require_scope, scope_query


class ContentService:
    """内容业务逻辑层。"""

    def __init__(self, db: Session):
        self.db = db
        self.content_repo = ContentRepository(db)

    def get_content(self, content_id: str, current_user: User) -> ContentItem:
        """获取内容详情（租户隔离）。

        Args:
            content_id: 内容 ID
            current_user: 当前用户

        Returns:
            内容条目

        Raises:
            HTTPException: 内容不存在或无权访问
        """
        item = self.content_repo.get_by_id(content_id)
        if item is None:
            raise HTTPException(status_code=404, detail="内容不存在")

        require_scope(item, current_user)
        if current_user.role == "viewer" and item.status != "published":
            raise HTTPException(status_code=403, detail="查看者仅能读取已发布内容")

        return item

    def list_contents(
        self,
        current_user: User,
        template_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ContentListOut:
        """内容列表查询（租户隔离 + 状态过滤）。

        Args:
            current_user: 当前用户
            template_id: 按题型过滤（可选）
            status: 按状态过滤（可选）
            page: 页码（从 1 开始）
            page_size: 每页数量
        """
        query = scope_query(self.db.query(ContentItem), ContentItem, current_user)
        if current_user.role == "viewer":
            query = query.filter(ContentItem.status == "published")
        if template_id:
            query = query.filter(ContentItem.template_id == template_id)
        if status:
            query = query.filter(ContentItem.status == status)
        total = query.count()
        items = (
            query.order_by(ContentItem.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        templates = (
            {
                t.type_id: t
                for t in self.db.query(QuestionTemplate)
                .filter(QuestionTemplate.type_id.in_({item.template_id for item in items}))
                .all()
            }
            if items
            else {}
        )
        return ContentListOut(
            total=total,
            page=page,
            page_size=page_size,
            items=[self._content_out(item, templates.get(item.template_id)) for item in items],
        )

    @staticmethod
    def _content_out(item: ContentItem, template: QuestionTemplate | None) -> ContentOut:
        result = ContentOut.model_validate(item)
        if template is not None:
            result.validation_report = inspect_content(
                item.payload, template.output_schema, template.run_config
            ).model_dump()
        return result

    def content_out(self, item: ContentItem) -> ContentOut:
        """Current-rule read-only diagnostics; stored reports remain audit snapshots."""
        template = (
            self.db.query(QuestionTemplate)
            .filter(QuestionTemplate.type_id == item.template_id)
            .first()
        )
        return self._content_out(item, template)

    def publish_content(self, content_id: str, current_user: User) -> ContentItem:
        """发布内容（状态校验：仅 passed 可发布）。

        Args:
            content_id: 内容 ID
            current_user: 当前用户（记录发布人）

        Returns:
            更新后的内容条目

        Raises:
            HTTPException: 内容不存在
            ContentStateConflictError: 状态不允许发布
        """
        item = self.content_repo.get_by_id(content_id)
        if item is None:
            raise HTTPException(status_code=404, detail="内容不存在")

        require_scope(item, current_user)
        provenance = item.provenance or {}
        if provenance.get("require_review") and not (provenance.get("reference_review") or {}).get(
            "verified"
        ):
            raise ContentStateConflictError("发布前必须完成人工参考来源核验")
        # 状态前置检查：仅 passed 可发布
        if not can_transition_content(item.status, "published"):
            raise ContentStateConflictError(
                f"内容状态 {item.status} 无法发布，仅 passed 状态可发布"
            )

        require_valid_content(self.db, item)

        # 更新状态与发布信息
        self.content_repo.update(
            item,
            status="published",
            published_by=current_user.id,
            published_at=datetime.now(timezone.utc),
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    def update_content_status(self, content_id: str, status: str, **kwargs) -> ContentItem:
        """更新内容状态（供质检/工作流调用）。

        Args:
            content_id: 内容 ID
            status: 新状态
            **kwargs: 其他要更新的字段（如 qc_score、failure_reason）

        Raises:
            HTTPException: 内容不存在
        """
        item = self.content_repo.get_by_id(content_id)
        if item is None:
            raise HTTPException(status_code=404, detail="内容不存在")

        update_data = {"status": status, **kwargs}
        self.content_repo.update(item, **update_data)
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_by_task(self, task_id: str, skip: int = 0, limit: int = 50):
        """查询任务的所有内容条目。"""
        return self.content_repo.list_by_task(task_id, skip, limit)

    def count_by_task(self, task_id: str) -> int:
        """统计任务的内容数。"""
        return self.content_repo.count_by_task(task_id)
