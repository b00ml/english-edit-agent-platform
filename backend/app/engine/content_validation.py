"""Configuration-driven deterministic guards; never assert semantic answer uniqueness."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import ContentValidationError


class ChoiceSetRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = ""
    options_field: str = "options"
    answer_field: str = "answer"
    index_field: str | None = None
    passage_field: str | None = None
    blank_marker: str = "____"
    labels: list[str] = Field(default_factory=lambda: ["A", "B", "C", "D"], min_length=2)
    position_warning_min: int = Field(default=5, ge=2)

    @model_validator(mode="after")
    def coherent(self) -> ChoiceSetRule:
        if self.labels != list(dict.fromkeys(self.labels)) or any(
            len(label) != 1 or not label.isascii() or not label.isupper() for label in self.labels
        ):
            raise ValueError("labels 必须是非重复的大写 ASCII 单字母")
        if not self.blank_marker or (self.passage_field and not self.path):
            raise ValueError("空格标记不能为空；passage_field 必须对应一个小题数组 path")
        if any(not field for field in (self.options_field, self.answer_field)):
            raise ValueError("options_field/answer_field 不能为空")
        if any(not part for part in self.path.split(".")) and self.path:
            raise ValueError("path 必须是点分隔的对象字段路径")
        return self


class ContentRules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choice_sets: list[ChoiceSetRule] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    code: str
    path: str
    message: str


class ValidationReport(BaseModel):
    valid: bool = True
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    semantic_verification: str = "requires_human_review"


def load_content_rules(run_config: dict[str, Any] | None) -> ContentRules:
    # Any is confined to the YAML/JSON schema configuration boundary.
    return ContentRules.model_validate((run_config or {}).get("content_validation", {}))


def _lookup(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split(".") if path else []:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def inspect_content(
    payload: dict[str, Any],
    schema: dict[str, Any],
    run_config: dict[str, Any] | None,
) -> ValidationReport:
    """Read-only diagnostics against current rules, including historical payloads."""
    rules = load_content_rules(run_config)
    report = ValidationReport()

    def error(code: str, path: str, message: str) -> None:
        report.errors.append(ValidationIssue(code=code, path=path, message=message))

    validator = jsonschema.validators.validator_for(schema)(schema)
    for i, exc in enumerate(validator.iter_errors(payload)):
        if i >= 12:
            break
        error("schema", ".".join(map(str, exc.absolute_path)) or "$", exc.message)

    for rule in rules.choice_sets:
        value = _lookup(payload, rule.path)
        entries = value if rule.path else [value]
        if not isinstance(entries, list) or not entries:
            error("choice_set", rule.path or "$", "小题集合必须是非空数组")
            continue
        answers: list[str] = []
        for offset, entry in enumerate(entries):
            path = f"{rule.path}.{offset}" if rule.path else "$"
            if not isinstance(entry, dict):
                error("choice_object", path, "小题必须是对象")
                continue
            if rule.index_field:
                index = entry.get(rule.index_field)
                if type(index) is not int or index != offset + 1:
                    error(
                        "index_sequence",
                        f"{path}.{rule.index_field}",
                        "序号必须按数组顺序从 1 连续递增且不重复",
                    )
            options = entry.get(rule.options_field)
            if not isinstance(options, list) or len(options) != len(rule.labels):
                error(
                    "option_count",
                    f"{path}.{rule.options_field}",
                    f"必须恰有 {len(rule.labels)} 个选项",
                )
                continue
            normalized: list[str] = []
            prefix = re.compile(r"^\s*[" + re.escape("".join(rule.labels)) + r"][.、):：]\s*")
            for pos, option in enumerate(options):
                option_path = f"{path}.{rule.options_field}.{pos}"
                if not isinstance(option, str) or not option.strip():
                    error("empty_option", option_path, "选项必须是非空文本")
                    continue
                text = unicodedata.normalize("NFKC", option)
                if prefix.match(text):
                    error(
                        "option_label", option_path, "options 仅存选项正文，不含 A./B. 等序号前缀"
                    )
                normalized.append(" ".join(text.split()).casefold())
            if len(normalized) != len(set(normalized)):
                error(
                    "duplicate_option",
                    f"{path}.{rule.options_field}",
                    "选项去除空白、大小写和全半角差异后不得重复",
                )
            answer = entry.get(rule.answer_field)
            if not isinstance(answer, str) or answer not in rule.labels:
                error(
                    "answer_label",
                    f"{path}.{rule.answer_field}",
                    f"答案必须是 {rule.labels} 之一，不能用词或带标点的序号",
                )
            else:
                answers.append(answer)
        if rule.passage_field:
            passage = _lookup(payload, rule.passage_field)
            marker = re.escape(rule.blank_marker)
            pattern = rf"(?<!_)({marker})(?!_)" if "_" in rule.blank_marker else marker
            count = len(re.findall(pattern, passage)) if isinstance(passage, str) else 0
            if count != len(entries):
                error(
                    "blank_count",
                    rule.passage_field,
                    f"短文空格数 {count} 必须等于小题数 {len(entries)}",
                )
            if (
                isinstance(passage, str)
                and rule.blank_marker == "____"
                and any(len(run) != 4 for run in re.findall(r"_{2,}", passage))
            ):
                error(
                    "blank_marker",
                    rule.passage_field,
                    "所有空格须使用恰好四个下划线 ____，不可混用长度",
                )
        if (
            len(answers) == len(entries)
            and len(answers) >= rule.position_warning_min
            and len(set(answers)) == 1
        ):
            report.warnings.append(
                ValidationIssue(
                    code="answer_position_bias",
                    path=rule.path or "$",
                    message=f"{len(answers)} 道小题答案全部集中在 {answers[0]}；请人工检查位置偏置及干扰项，不强制短批次均匀分布",
                )
            )
    report.valid = not report.errors
    return report


def enforce_content(
    payload: dict[str, Any],
    schema: dict[str, Any],
    run_config: dict[str, Any] | None,
) -> ValidationReport:
    report = inspect_content(payload, schema, run_config)
    if not report.valid:
        raise ContentValidationError(report)
    return report
