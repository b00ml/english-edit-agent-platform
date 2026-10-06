"""配置版本快照与内容寻址哈希工具。

所有进入任务和质检记录的配置先做稳定 JSON 序列化，再计算 SHA-256，避免
Python dict 插入顺序或 YAML 排版变化导致同一配置得到不同指纹。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """将动态配置序列化为稳定、可审计的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_value(value: Any) -> str:
    """计算任意 JSON 兼容值的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_file(path: Path) -> str:
    """计算 Prompt/Skill 文件原始字节摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skill_hash(type_id: str) -> str | None:
    """读取题型 Skill 摘要；Skill 不存在时返回 None。"""
    skill_path = Path(__file__).parent.parent / "skills" / type_id / "SKILL.md"
    if not skill_path.exists():
        return None
    return hash_file(skill_path)


def _prompt_hash(prompt_config: Any) -> str:
    """按模板引用的 system/user 文件内容计算 Prompt 摘要。"""
    prompt_dir = Path(__file__).parent.parent / "prompts"
    files: dict[str, str] = {}
    if isinstance(prompt_config, dict):
        for key in ("system", "user"):
            name = prompt_config.get(key)
            if not isinstance(name, str):
                continue
            prompt_path = prompt_dir / name.removeprefix("prompts/")
            if prompt_path.exists():
                files[key] = hash_file(prompt_path)
            else:
                files[key] = hash_value(name)
    return hash_value(files)


def template_hashes(data: dict[str, Any]) -> dict[str, str | None]:
    """计算模板、Prompt、Skill 三类摘要。"""
    prompt = data.get("gen_prompt") or {}
    template_payload = {
        "type_id": data.get("type_id"),
        "version": data.get("version"),
        "input_schema": data.get("input_schema"),
        "output_schema": data.get("output_schema"),
        "quality_rules": data.get("quality_rules"),
        "gen_prompt": prompt,
        "run_config": data.get("run_config"),
    }
    return {
        "template_hash": hash_value(template_payload),
        "prompt_hash": _prompt_hash(prompt),
        "skill_hash": _skill_hash(str(data.get("type_id", ""))),
    }


def template_snapshot(template: Any) -> dict[str, Any]:
    """从 ORM 模板生成可写入任务/质检记录的不可变版本快照。"""
    return {
        "template_id": template.type_id,
        "version": getattr(template, "version", 1),
        "template_hash": getattr(template, "template_hash", None),
        "prompt_hash": getattr(template, "prompt_hash", None),
        "skill_hash": getattr(template, "skill_hash", None),
        "quality_rules": getattr(template, "quality_rules", None) or [],
        "run_config": template.run_config or {},
    }


def quality_snapshot(
    template: Any,
    *,
    model_name: str | None,
    threshold: float | None = None,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成自动/人工质检共享的配置快照。"""
    snapshot = template_snapshot(template)
    snapshot.update(
        {
            "model_name": model_name,
            "judge_prompt_hash": _prompt_hash(
                {"system": "judge-system.st", "user": "judge-user.st"}
            ),
            "judge_skill_hash": _skill_hash("judge"),
            "threshold": threshold,
            "weights": weights,
        }
    )
    return snapshot


def task_version_snapshot(template: Any) -> dict[str, Any]:
    """生成任务创建时的模板版本快照。"""
    return template_snapshot(template)


def model_profile_hash(profile: Any) -> str:
    """对模型档案的可执行配置计算稳定摘要。"""
    return hash_value(
        {
            "name": profile.name,
            "provider": profile.provider,
            "provider_id": getattr(profile, "provider_id", None),
            "provider_config_hash": getattr(profile, "provider_config_hash", None),
            "model_name": profile.model_name,
            "cost_tier": profile.cost_tier,
            "is_default": profile.is_default,
            "status": profile.status,
            "max_fallbacks": profile.max_fallbacks,
            "budget_per_task": profile.budget_per_task,
        }
    )


def ensure_model_profile_hash(profile: Any) -> str:
    """按当前可执行配置刷新摘要，避免更新后沿用旧 hash。"""
    value = model_profile_hash(profile)
    profile.model_hash = value
    return value


def task_version_snapshot_with_model(template: Any, profile: Any | None = None) -> dict[str, Any]:
    """生成同时固化模板和实际模型档案的任务版本快照。"""
    snapshot = template_snapshot(template)
    if profile is not None:
        snapshot["model"] = {
            "name": profile.name,
            "provider": profile.provider,
            "provider_id": getattr(profile, "provider_id", None),
            "provider_config_hash": getattr(profile, "provider_config_hash", None),
            "model_name": profile.model_name,
            "model_hash": getattr(profile, "model_hash", None) or model_profile_hash(profile),
        }
    return snapshot
