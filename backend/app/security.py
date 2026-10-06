# app/security.py —— 认证与授权（K4 权限）
# 职责：
#   1. 密码哈希/校验（bcrypt，绝不落明文）；
#   2. JWT 签发/解析（无状态会话）；
#   3. 角色权限矩阵 + FastAPI 依赖注入（get_current_user / require_permission）。
# 说明：权限校验以代码强制兜底，前端仅做 UX 隐藏，安全边界在后端。
from datetime import datetime, timedelta, timezone
from typing import List

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

# 密码哈希上下文（bcrypt）
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Bearer token 提取器
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# 角色与权限矩阵
# ---------------------------------------------------------------------------
# 权限点定义（映射 PRD 第 13 节）。权限点 -> 允许的角色集合。
ROLE_PERMISSIONS: dict[str, List[str]] = {
    "model:manage": ["admin"],
    # 用户管理（仅管理员）
    "user:manage": ["admin"],
    # 题型模板 / 模型路由配置（仅管理员）
    "template:manage": ["admin"],
    # 发起生成
    "generate:create": ["admin", "researcher"],
    # 取消生成任务
    "generate:cancel": ["admin", "researcher"],
    # 查看任务 / 内容（已发布内容对查看者开放，见 apply_scope）
    "task:read": ["admin", "researcher"],
    "content:read": ["admin", "researcher", "reviewer", "viewer"],
    # 人工质检标注
    "quality:review": ["admin", "researcher", "reviewer"],
    # 发布内容
    "content:publish": ["admin", "researcher"],
    # 成本 / 看板 / 链路 / 知识库 / 样本库（内部运营能力）
    "ops:read": ["admin", "researcher"],
    # 知识库 / 样本库写操作
    "ops:write": ["admin", "researcher"],
}

# 角色 -> 允许访问的前端导航页面（与前端 NAV_ITEMS 对应，供前端按角色过滤）
ROLE_PAGES: dict[str, List[str]] = {
    "admin": [
        "/",
        "/tasks",
        "/quality",
        "/library",
        "/samples",
        "/knowledge",
        "/dashboard",
        "/costs",
        "/traces",
        "/notifications",
        "/users",
        "/model-settings",
    ],
    "researcher": [
        "/",
        "/tasks",
        "/quality",
        "/library",
        "/samples",
        "/knowledge",
        "/dashboard",
        "/costs",
        "/traces",
        "/notifications",
    ],
    "reviewer": ["/quality", "/library", "/notifications"],
    "viewer": ["/library", "/notifications"],
}


def has_permission(user: User, permission: str) -> bool:
    """判断用户是否拥有指定权限点。"""
    allowed = ROLE_PERMISSIONS.get(permission, [])
    return user.status == "active" and user.role in allowed


# ---------------------------------------------------------------------------
# 密码哈希 / 校验
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """对明文密码做 bcrypt 哈希。"""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。"""
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001 —— 哈希格式异常一律视为校验失败
        return False


# ---------------------------------------------------------------------------
# JWT 签发 / 解析
# ---------------------------------------------------------------------------
def create_access_token(user: User) -> str:
    """为用户签发 JWT。payload 仅含身份标识，不携带敏感信息。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(seconds=settings.JWT_EXPIRE_SECONDS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并校验 JWT，无效/过期抛 401 语义的 HTTPException。"""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效的登录凭证")


# ---------------------------------------------------------------------------
# FastAPI 依赖注入
# ---------------------------------------------------------------------------
def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """从 Bearer token 解析当前登录用户；未认证或已禁用则 401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_access_token(credentials.credentials)
    user = db.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    if user.status != "active":
        raise HTTPException(status_code=401, detail="账号已被禁用")
    return user


def require_permission(permission: str):
    """构造一个依赖：校验当前用户是否拥有指定权限点，否则 403。"""

    def _checker(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, permission):
            raise HTTPException(status_code=403, detail="无权执行该操作")
        return user

    return _checker
