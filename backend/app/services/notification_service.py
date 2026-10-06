"""Notifications are tenant broadcasts; the model does not contain a user_id."""

from __future__ import annotations

from typing import TypedDict

from sqlalchemy.orm import Session

from app.models import AppNotification, User
from app.tenancy import require_scope, scope_query


class NotificationList(TypedDict):
    items: list[AppNotification]
    total: int


class NotificationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_notifications(
        self, user: User, skip: int = 0, limit: int = 50, unread_only: bool = False
    ) -> NotificationList:
        query = scope_query(self.db.query(AppNotification), AppNotification, user)
        if unread_only:
            query = query.filter(AppNotification.is_read.is_(False))
        return {
            "items": query.order_by(AppNotification.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all(),
            "total": query.count(),
        }

    def get_unread_count(self, user: User) -> int:
        return (
            scope_query(self.db.query(AppNotification), AppNotification, user)
            .filter(AppNotification.is_read.is_(False))
            .count()
        )

    def mark_as_read(self, notification_id: str, user: User) -> dict[str, object]:
        row = require_scope(self.db.get(AppNotification, notification_id), user)
        row.is_read = True
        self.db.commit()
        self.db.refresh(row)
        return {
            "id": row.id,
            "type": row.type,
            "title": row.title,
            "content": row.content,
            "related_id": row.related_id,
            "is_read": row.is_read,
            "created_at": row.created_at,
        }

    def mark_all_as_read(self, user: User) -> int:
        count = (
            scope_query(self.db.query(AppNotification), AppNotification, user)
            .filter(AppNotification.is_read.is_(False))
            .update({"is_read": True}, synchronize_session=False)
        )
        self.db.commit()
        return count
