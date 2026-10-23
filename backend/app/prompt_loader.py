# app/prompt_loader.py —— 独立 prompt 加载与渲染
# prompt 以 .st 文件独立存放（backend/prompts/），system/user 分离，便于版本控制与调试。
# 生成时按题型加载对应 skill 规范注入 system prompt，作为「出题技能」约束。
import json
import os
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Dict

_FROZEN_ASSETS: ContextVar[dict[str, Any] | None] = ContextVar("frozen_assets", default=None)


def teaching_constraints(params: dict[str, Any]) -> dict[str, Any]:
    """Allow only teaching requirements, never content answers or evaluation outcomes."""
    return {
        key: params[key]
        for key in (
            "grade",
            "difficulty",
            "knowledge_point",
            "topic",
            "constraints",
            "quantity",
            "blank_count",
            "question_count",
            "teaching_goal",
            "scope",
            "judgment_basis",
        )
        if key in params
    }


def frozen_skill(skill_id: str, *, fallback: bool = False) -> str | None:
    """Resolve an evaluation skill from its snapshot; None means normal live execution."""
    assets = _FROZEN_ASSETS.get()
    if assets is None or "skills" not in assets:
        return None
    skills = assets["skills"]
    key = f"skills/{skill_id}/SKILL.md"
    value = skills.get(key, skills.get("skills/generate_question/SKILL.md", "") if fallback else "")
    if not isinstance(value, str):
        raise ValueError("Frozen skill must be text")
    return value


def with_frozen_assets(function: Callable[..., Any]) -> Callable[..., Any]:
    """Scope all generation/Judge/reference prompt reads to one immutable snapshot."""

    @wraps(function)
    def scoped(path: Any, manifest: dict[str, Any], **kwargs: Any) -> Any:
        token = _FROZEN_ASSETS.set(manifest["frozen"])
        try:
            return function(path, manifest, **kwargs)
        finally:
            _FROZEN_ASSETS.reset(token)

    return scoped


# prompts 目录（backend/prompts）
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


def load_prompt(rel_path: str) -> str:
    """加载 prompts/ 下的 .st 文件内容，安全解析相对路径前缀。"""
    if rel_path.startswith("prompts/"):
        rel_path = rel_path[len("prompts/") :]
    assets = _FROZEN_ASSETS.get()
    if assets is not None and "prompts" in assets:
        return str(assets["prompts"][rel_path])
    filepath = os.path.join(PROMPTS_DIR, rel_path)
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def render(template: str, params: Dict[str, Any]) -> str:
    """用 {{var}} 占位符替换渲染 prompt；缺失变量原样保留。"""
    for key, value in params.items():
        template = template.replace("{{" + str(key) + "}}", str(value))
    return template


def build_system_prompt(template: Any, params: Dict[str, Any], skill_md: str = "") -> str:
    """构造 system prompt = 基础 system .st 渲染 + 出题技能规范。

    skill_md 为 skill/SKILL.md 内容，作为面向模型的出题约束注入。
    """
    system = render(load_prompt(template.gen_prompt["system"]), params)
    if skill_md:
        system = f"{system}\n\n# 出题技能规范\n{skill_md}"
    return system


def build_user_prompt(template: Any, params: Dict[str, Any]) -> str:
    """构造 user prompt = user .st 渲染 + 可选 RAG 参考资料。"""
    user = render(load_prompt(template.gen_prompt["user"]), params)
    teaching = teaching_constraints(params)
    if teaching:
        user += "\n\n" + render(
            load_prompt("teaching-constraints.st"),
            {"teaching_constraints": json.dumps(teaching, ensure_ascii=False)},
        )
    examples = params.get("fewshot_context")
    if examples:
        user += "\n\n" + render(load_prompt("fewshot-instruction.st"), {"examples": examples})
    rag_context = params.get("rag_context")
    if rag_context:
        user = f"{user}\n\n参考资料（务必参考以增强内容的事实依据）：\n{rag_context}"
    return user
