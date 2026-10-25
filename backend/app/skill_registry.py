# app/skill_registry.py —— 技能注册表
# 从 backend/skills/<skill_id>/SKILL.md 加载技能：
#   - 出题技能按题型 type_id 匹配（缺失回退 generate_question）
#   - 通用技能（如 judge 质检）按 skill_id 直接读取（不回退）
# skill 与题型解耦：新增题型只需新增同名 skill 目录，不改代码。
import os
from functools import lru_cache

from app.prompt_loader import frozen_skill

# skills 目录（backend/skills）
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "..", "skills")
# 兜底技能：题型无专属技能时使用的通用出题规范
_DEFAULT_SKILL = "generate_question"


def get_skill_md(type_id: str) -> str:
    frozen = frozen_skill(type_id, fallback=True)
    return frozen if frozen is not None else _get_skill_md(type_id)


@lru_cache(maxsize=None)
def _get_skill_md(type_id: str) -> str:
    """读取 skills/{type_id}/SKILL.md 内容。

    优先匹配题型同名技能；缺失时回退到通用技能 generate_question；
    仍无则返回空串（调用方不注入技能规范）。
    """
    for candidate in (type_id, _DEFAULT_SKILL):
        path = os.path.join(SKILLS_DIR, candidate, "SKILL.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def get_skill_by_id(skill_id: str) -> str:
    frozen = frozen_skill(skill_id)
    return frozen if frozen is not None else _get_skill_by_id(skill_id)


@lru_cache(maxsize=None)
def _get_skill_by_id(skill_id: str) -> str:
    """按 skill_id 直接读取 skills/{skill_id}/SKILL.md，不做回退。

    用于跨题型的通用技能（如 judge 质检），与题型 type_id 解耦。
    不存在时返回空串。
    """
    path = os.path.join(SKILLS_DIR, skill_id, "SKILL.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def clear_cache() -> None:
    """清空技能缓存（开发/测试用，便于重载）。"""
    _get_skill_md.cache_clear()
    _get_skill_by_id.cache_clear()
