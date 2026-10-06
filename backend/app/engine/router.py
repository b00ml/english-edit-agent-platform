# app/engine/router.py —— 模型路由与降级
# 按 run_config.model_profile 路由到主模型；主模型失败时降级到备用/默认模型。
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.engine.providers import route_for_template
from app.engine.structured_output import generate_structured
from app.engine.trace_recovery import pending_task_cost
from app.errors import ModelRoutingError
from app.models import ModelProfile, TraceLog


def resolve_model_profile(
    session: Session, profile_name: Optional[str], tenant_id: str | None = None
) -> Optional[ModelProfile]:
    """按 profile_name 解析模型档案，找不到时降级到 is_default 的默认档案。

    - 优先精确匹配 profile_name；
    - 未匹配则返回 is_default=True 的记录；
    - 均无则返回 None（由调用方决定使用内置默认模型名）。
    """
    if profile_name:
        profile = session.execute(
            select(ModelProfile).where(
                ModelProfile.name == profile_name,
                or_(ModelProfile.tenant_id == tenant_id, ModelProfile.tenant_id.is_(None)),
            )
        ).scalar_one_or_none()
        if profile and _is_eligible(profile):
            return profile

    # 降级到默认档案
    default = (
        session.execute(
            select(ModelProfile)
            .where(
                ModelProfile.is_default.is_(True),
                or_(ModelProfile.tenant_id == tenant_id, ModelProfile.tenant_id.is_(None)),
            )
            .order_by(ModelProfile.tenant_id.is_(None))
        )
        .scalars()
        .first()
    )
    if default and _is_eligible(default):
        return default
    return None


def _is_eligible(profile: ModelProfile) -> bool:
    """模型只有启用且不在冷却期才可参与路由。"""
    if getattr(profile, "status", "enabled") != "enabled":
        return False
    cooldown_until = getattr(profile, "cooldown_until", None)
    if cooldown_until is None:
        return True
    now = datetime.now(timezone.utc)
    if cooldown_until.tzinfo is None:
        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
    return cooldown_until <= now


def _task_cost(session: Session, task_id: Optional[str]) -> float:
    """读取任务已落库的调用成本，用于下一次调用前的预算守卫。"""
    if not task_id:
        return 0.0
    value = (
        session.execute(select(TraceLog.cost).where(TraceLog.task_id == task_id)).scalars().all()
    )
    return sum(float(v or 0.0) for v in value) + pending_task_cost(task_id)


def _mark_success(session: Session, profile: ModelProfile) -> None:
    profile.failure_count = 0
    profile.health_status = "healthy"
    profile.cooldown_until = None
    profile.last_health_check_at = datetime.now(timezone.utc)
    session.commit()


def _mark_failure(session: Session, profile: ModelProfile) -> None:
    profile.failure_count = int(profile.failure_count or 0) + 1
    profile.last_health_check_at = datetime.now(timezone.utc)
    if profile.failure_count >= settings.MODEL_FAILURE_THRESHOLD:
        profile.health_status = "unhealthy"
        profile.cooldown_until = datetime.now(timezone.utc) + timedelta(
            seconds=settings.MODEL_COOLDOWN_SECONDS
        )
    else:
        profile.health_status = "degraded"
    session.commit()


def _resolve_primary_profile_name(
    run_config: dict,
    params: dict,
    profile_name: Optional[str] = None,
) -> Optional[str]:
    """解析本次生成的主模型档案名。

    `run_config.model_profile` 支持两种形态（配置驱动，新增分档不改代码）：

    - 字符串（历史兼容）：直接作为档案名；
    - 字典（按难度分档）：按 `params.difficulty` 选对应档位，
      缺档优先回退 `default` 键，再回退到任意一个档位值。

    显式传入的 `profile_name` 优先于 run_config。
    """
    effective = profile_name if profile_name is not None else run_config.get("model_profile")
    if isinstance(effective, str):
        return effective
    if isinstance(effective, dict):
        difficulty = params.get("difficulty")
        if difficulty is not None and difficulty in effective:
            return effective[difficulty]
        if "default" in effective:
            return effective["default"]
        if effective:
            return next(iter(effective.values()))
    return None


def _get_named_profile(
    session: Session, name: Optional[str], tenant_id: str | None = None
) -> Optional[ModelProfile]:
    """精确查询档案，不隐式替换为默认档案。"""
    if not name:
        return None
    return session.execute(
        select(ModelProfile).where(
            ModelProfile.name == name,
            or_(ModelProfile.tenant_id == tenant_id, ModelProfile.tenant_id.is_(None)),
        )
    ).scalar_one_or_none()


def generate_with_fallback(
    template: Any,
    params: dict,
    session: Session,
    profile_name: Optional[str] = None,
    trace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    template_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> dict:
    """主模型失败后降级到备用/默认模型的生成入口。

    路由顺序：按 run_config.model_profile（含难度分档）解析出的主模型
      -> 降级到 is_default 默认模型 -> 使用内置默认模型名（settings.LLM_MODEL_NAME）。
    任一成功即返回；全部失败抛异常。
    """
    run_config = template.run_config or {}
    override = (
        route_for_template(session, template.type_id)
        if isinstance(session, Session) and getattr(template, "type_id", None)
        else None
    )
    if override is not None and override.generation_profile:
        profile_name = override.generation_profile
    primary_name = _resolve_primary_profile_name(run_config, params, profile_name)

    # 候选模型档案列表（主 -> 默认）。这里必须精确查询主档案，避免配置拼写
    # 错误被静默替换为默认模型，导致 trace 中无法识别实际执行配置。
    candidates: list[Optional[ModelProfile]] = []
    primary = _get_named_profile(session, primary_name, tenant_id)
    candidates.append(primary)

    default = resolve_model_profile(session, None, tenant_id)
    if default is not None and default.id != (primary.id if primary else None):
        candidates.append(default)

    # 去重（避免主/默认相同导致重复尝试同一次降级）
    seen: set = set()
    last_error: Optional[Exception] = None
    fallback_count = 0
    budget = settings.MODEL_BUDGET_PER_TASK
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        if not _is_eligible(candidate):
            continue
        profile_budget = candidate.budget_per_task or budget
        if profile_budget > 0 and _task_cost(session, task_id) >= profile_budget:
            raise ModelRoutingError(f"任务 {task_id} 已达到模型预算上限 {profile_budget}")
        try:
            result = generate_structured(
                template,
                params,
                candidate,
                trace_id=trace_id,
                task_id=task_id,
                template_id=template_id,
                tenant_id=tenant_id,
            )
            _mark_success(session, candidate)
            return result
        except Exception as exc:  # noqa: BLE001 —— 降级需捕获任意生成异常
            last_error = exc
            _mark_failure(session, candidate)
            fallback_count += 1
            max_fallbacks = min(
                settings.MODEL_MAX_FALLBACKS,
                int(
                    candidate.max_fallbacks
                    if candidate.max_fallbacks is not None
                    else settings.MODEL_MAX_FALLBACKS
                ),
            )
            if fallback_count > max_fallbacks:
                break
            continue

    reason = last_error or RuntimeError("无可用模型档案（已禁用或仍在冷却）")
    raise ModelRoutingError(
        f"模型路由降级后仍生成失败，最后错误类型: {type(reason).__name__}", reason
    )
