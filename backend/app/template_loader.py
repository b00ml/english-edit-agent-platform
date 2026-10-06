# app/template_loader.py —— 题型模板加载器
# 从 backend/app/templates/*.yaml 读取题型模板，校验字段后按 type_id upsert 入库。
import glob
import os
from typing import List

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.audit import record_config_change
from app.engine.content_validation import load_content_rules
from app.models import QuestionTemplate
from app.versioning import template_hashes, template_snapshot

# 模板文件目录（相对本文件）
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# 必填字段，缺失或非法即视为加载失败
REQUIRED_FIELDS = [
    "type_id",
    "name",
    "version",
    "input_schema",
    "output_schema",
    "quality_rules",
    "gen_prompt",
    "run_config",
]

# 允许的取值范围（用于基本合法性校验）
_VALID_STATUS = {"enabled", "disabled"}


def _validate_template(data: dict) -> List[str]:
    """校验模板字段是否齐全且格式合法，返回错误信息列表（空表示通过）。"""
    errors: List[str] = []

    for field in REQUIRED_FIELDS:
        if field not in data:
            errors.append(f"缺少必填字段: {field}")

    # 已缺失则下述校验可能报 AttributeError，提前返回
    if errors:
        return errors

    if not isinstance(data["version"], int) or data["version"] < 1:
        errors.append("version 必须为不小于 1 的整数")

    # input_schema / output_schema 必须是 JSON Schema 对象
    for schema_field in ("input_schema", "output_schema"):
        value = data[schema_field]
        if not isinstance(value, dict) or value.get("type") not in ("object", "array"):
            errors.append(f"{schema_field} 必须是 JSON Schema 对象")

    # quality_rules 必须是列表，且每条含 id 与 weight
    quality_rules = data["quality_rules"]
    weight_sum_ok = True
    if not isinstance(quality_rules, list) or not quality_rules:
        errors.append("quality_rules 必须是非空列表")
        weight_sum_ok = False
    else:
        total_weight = 0.0
        for rule in quality_rules:
            if not isinstance(rule, dict) or "id" not in rule or "weight" not in rule:
                errors.append("quality_rules 每条必须含 id 与 weight")
                weight_sum_ok = False
            else:
                total_weight += float(rule["weight"])
        # 仅当所有规则结构合法时才校验权重和应接近 1.0（允许浮点误差）
        if weight_sum_ok and abs(total_weight - 1.0) > 1e-6:
            errors.append(f"quality_rules 权重之和应为 1.0，当前为 {total_weight}")

    # gen_prompt 必须含 system 与 user
    gen_prompt = data["gen_prompt"]
    if not isinstance(gen_prompt, dict) or "system" not in gen_prompt or "user" not in gen_prompt:
        errors.append("gen_prompt 必须含 system 与 user 字段")

    # run_config 必须含 model_profile 与 max_retry
    run_config = data["run_config"]
    if not isinstance(run_config, dict) or "model_profile" not in run_config:
        errors.append("run_config 必须含 model_profile 字段")

    if isinstance(run_config, dict):
        try:
            load_content_rules(run_config)
        except ValidationError as exc:
            errors.append(f"content_validation 配置非法: {exc}")

    fewshot = run_config.get("fewshot", {}) if isinstance(run_config, dict) else {}
    if not isinstance(fewshot, dict) or set(fewshot) - {"enabled", "max_examples", "max_chars"}:
        errors.append("fewshot 配置格式不合法")
    elif ("enabled" in fewshot and type(fewshot["enabled"]) is not bool) or any(
        type(fewshot[key]) is not int or not low <= fewshot[key] <= high
        for key, low, high in (("max_examples", 0, 5), ("max_chars", 256, 32768))
        if key in fewshot
    ):
        errors.append("fewshot enabled/数量/字符预算不合法")

    # status 可选，若提供则必须在合法集合内
    if "status" in data and data["status"] not in _VALID_STATUS:
        errors.append(f"status 必须是 {_VALID_STATUS} 之一")

    return errors


def load_all_templates(session: Session) -> int:
    """加载 templates/ 下所有 YAML 模板并 upsert 入库，返回加载数量。

    按 type_id 去重：已存在则更新，不存在则新建。
    任何单个模板校验失败都会抛出异常并终止加载（保证数据一致性）。
    """
    loaded = 0
    pattern = os.path.join(TEMPLATES_DIR, "*.yaml")
    for filepath in sorted(glob.glob(pattern)):
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"模板文件 {filepath} 内容不是 YAML 对象")

        errors = _validate_template(data)
        if errors:
            raise ValueError(f"模板文件 {filepath} 校验失败: {'; '.join(errors)}")

        type_id = data["type_id"]
        # 按 type_id 查询，存在则更新，否则新建（upsert 语义）
        template = (
            session.query(QuestionTemplate).filter(QuestionTemplate.type_id == type_id).first()
        )
        if template is None:
            template = QuestionTemplate(type_id=type_id)
            session.add(template)
            before_snapshot = None
        else:
            before_snapshot = template_snapshot(template)

        # 更新字段
        template.name = data["name"]
        template.version = data["version"]
        template.input_schema = data["input_schema"]
        template.output_schema = data["output_schema"]
        template.quality_rules = data["quality_rules"]
        template.gen_prompt = data["gen_prompt"]
        template.run_config = data["run_config"]
        template.status = data.get("status", "enabled")
        hashes = template_hashes(data)
        template.template_hash = hashes["template_hash"]
        template.prompt_hash = hashes["prompt_hash"]
        template.skill_hash = hashes["skill_hash"]
        record_config_change(
            session,
            entity_type="question_template",
            entity_id=type_id,
            action="create" if before_snapshot is None else "sync",
            actor_id=None,
            tenant_id=template.tenant_id,
            before=before_snapshot,
            after=template_snapshot(template),
        )

        loaded += 1

    session.commit()
    return loaded
