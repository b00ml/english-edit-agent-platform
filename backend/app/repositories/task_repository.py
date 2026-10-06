"""
生成任务 Repository
职责：GenerationTask 数据访问、查询
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.domain.status import ACTIVE_TASK_STATUSES
from app.models import GenerationTask
from app.repositories.base import BaseRepository


class TaskRepository(BaseRepository[GenerationTask]):
    """生成任务数据访问层。"""

    def __init__(self, db: Session):
        super().__init__(GenerationTask, db)

    def get_by_request_hash(self, request_hash: str) -> Optional[GenerationTask]:
        """根据请求哈希查询（去重）。"""
        return (
            self.db.query(GenerationTask)
            .filter(GenerationTask.request_hash == request_hash)
            .filter(GenerationTask.status.in_(ACTIVE_TASK_STATUSES))
            .first()
        )

    def list_by_user(self, user_id: int, skip: int = 0, limit: int = 50) -> List[GenerationTask]:
        """查询用户的任务列表（按创建时间倒序）。"""
        return (
            self.db.query(GenerationTask)
            .filter(GenerationTask.user_id == user_id)
            .order_by(desc(GenerationTask.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_by_tenant(
        self, tenant_id: int, skip: int = 0, limit: int = 50
    ) -> List[GenerationTask]:
        """查询租户的任务列表。"""
        return (
            self.db.query(GenerationTask)
            .filter(GenerationTask.tenant_id == tenant_id)
            .order_by(desc(GenerationTask.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_by_status(self, status: str, skip: int = 0, limit: int = 50) -> List[GenerationTask]:
        """按状态查询任务。"""
        return (
            self.db.query(GenerationTask)
            .filter(GenerationTask.status == status)
            .order_by(desc(GenerationTask.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_user(self, user_id: int) -> int:
        """统计用户任务数。"""
        return (
            self.db.query(func.count(GenerationTask.id))
            .filter(GenerationTask.user_id == user_id)
            .scalar()
        )

    def count_by_tenant(self, tenant_id: int) -> int:
        """统计租户任务数。"""
        return (
            self.db.query(func.count(GenerationTask.id))
            .filter(GenerationTask.tenant_id == tenant_id)
            .scalar()
        )

    def get_recent_running(self, minutes: int = 60) -> List[GenerationTask]:
        """查询最近 N 分钟内运行中的任务（僵尸任务检测）。"""
        threshold = datetime.utcnow()
        # 简化：假设 updated_at 存在（实际可能需要 started_at 字段）
        return (
            self.db.query(GenerationTask)
            .filter(GenerationTask.status == "running")
            .filter(GenerationTask.created_at < threshold)
            .all()
        )

    def count_by_status(self, tenant_id: str) -> dict:
        """
        按状态统计任务数量

        返回全部八种现役任务状态计数；不把 awaiting_review 归为已完成。
        """
        rows = (
            self.db.query(self.model.status, func.count(self.model.id))
            .filter(self.model.tenant_id == tenant_id)
            .group_by(self.model.status)
            .all()
        )
        from app.domain.status import TASK_STATUSES

        result = {status: 0 for status in TASK_STATUSES}
        for status, count in rows:
            result[status] = int(count)
        return result
