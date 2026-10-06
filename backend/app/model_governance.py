"""模型档案启动校验与模板引用解析。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import ModelProfile, QuestionTemplate


def referenced_profile_names(run_config: dict[str, Any]) -> set[str]:
    """提取模板 generation model_profile 的全部分档引用。"""
    configured = run_config.get("model_profile")
    if isinstance(configured, str):
        return {configured}
    if isinstance(configured, dict):
        return {value for value in configured.values() if isinstance(value, str)}
    return set()


def validate_template_model_references(session: Session) -> list[str]:
    """返回启用模板引用了不存在模型档案的错误列表。"""
    errors: list[str] = []
    templates = session.query(QuestionTemplate).filter(QuestionTemplate.status == "enabled").all()
    for template in templates:
        scoped_names = {
            name
            for (name,) in session.query(ModelProfile.name)
            .filter(
                or_(ModelProfile.tenant_id == template.tenant_id, ModelProfile.tenant_id.is_(None))
            )
            .all()
        }
        missing = referenced_profile_names(template.run_config or {}) - scoped_names
        if missing:
            errors.append(
                f"模板 {template.type_id} 引用了不存在的模型档案: {', '.join(sorted(missing))}"
            )
    return errors
