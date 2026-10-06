"""Explicit tenant scopes. None is the legacy default tenant, never a scope bypass."""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import or_
from sqlalchemy.orm import Query

from app.errors import NotFoundError, TenantScopeDeniedError
from app.models import Base, User

T = TypeVar("T")
B = TypeVar("B", bound=Base)


def scope_query(
    query: Query[T], model: type[Base], user: User | None, *, shared: bool = False
) -> Query[T]:
    """Only admin or trusted internal callers (no User) may query all tenants."""
    if user is None or user.role == "admin":
        return query
    column = getattr(model, "tenant_id")
    predicate = column == user.tenant_id
    if shared:
        predicate = or_(predicate, column.is_(None))
    return query.filter(predicate)


def require_scope(record: B | None, user: User | None, *, shared: bool = False) -> B:
    if record is None:
        raise NotFoundError("记录不存在")
    if user is not None and user.role != "admin":
        tenant = getattr(record, "tenant_id")
        if tenant != user.tenant_id and not (shared and tenant is None):
            raise TenantScopeDeniedError("无权访问其他租户的记录")
    return record
