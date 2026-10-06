# app/prompt_loader.py —— 独立 prompt 加载与渲染
# prompt 以 .st 文件独立存放（backend/prompts/），system/user 分离，便于版本控制与调试。
# 生成时按题型加载对应 skill 规范注入 system prompt，作为「出题技能」约束。
import os
from typing import Any, Dict

# prompts 目录（backend/prompts）
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


def load_prompt(rel_path: str) -> str:
    """加载 prompts/ 下的 .st 文件内容，安全解析相对路径前缀。"""
    if rel_path.startswith("prompts/"):
        rel_path = rel_path[len("prompts/") :]
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
    examples = params.get("fewshot_context")
    if examples:
        user += "\n\n" + render(load_prompt("fewshot-instruction.st"), {"examples": examples})
    rag_context = params.get("rag_context")
    if rag_context:
        user = f"{user}\n\n参考资料（务必参考以增强内容的事实依据）：\n{rag_context}"
    return user
