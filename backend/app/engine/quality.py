# app/engine/quality.py —— LLM-as-judge 自动质检
# 按模板 quality_rules 逐维度打分（0-100），加权汇总；judge 多次采样取均值以降低抖动。
import json
import logging
import math
import time
import uuid
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI, OpenAIError
from pydantic import ConfigDict, Field, ValidationError, confloat, create_model
from sqlalchemy.orm import Session

from app.config import settings
from app.engine.providers import client_for_profile, profile_identity
from app.engine.trace import (
    compute_cost,
    elapsed_ms,
    record_trace,
    token_breakdown,
    usage_is_reported,
)
from app.errors import ProviderConfigurationError, QualityCheckError
from app.models import ModelProfile
from app.prompt_loader import load_prompt, render
from app.skill_registry import get_skill_by_id

logger = logging.getLogger("app.engine.quality")


def resolve_judge_model(
    template: Any, session: Session | None = None, tenant_id: str | None = None
) -> str:
    """解析 judge 模型（P1-2 自偏好治理）：模板 run_config.judge_model > 全局
    JUDGE_MODEL_NAME > LLM_MODEL_NAME。建议 judge 与生成主模型不同家族。
    """
    if isinstance(session, Session):
        from app.engine.providers import route_for_template
        from app.models import ModelProfile

        route = route_for_template(session, template.type_id)
        if route is not None and route.judge_profile:
            profile = (
                session.query(ModelProfile).filter(ModelProfile.name == route.judge_profile).first()
            )
            if (
                profile is None
                or profile.status != "enabled"
                or profile.tenant_id not in (None, tenant_id)
            ):
                raise QualityCheckError("配置的Judge档案不可用")
            return str(profile.model_name)
    run_config = getattr(template, "run_config", None) or {}
    return run_config.get("judge_model") or settings.JUDGE_MODEL_NAME or settings.LLM_MODEL_NAME


def _aggregate_score(
    rules: List[Dict[str, Any]],
    dimension_scores: Dict[str, float],
    weights_override: Optional[Dict[str, float]] = None,
) -> float:
    """按 quality_rules 权重对维度分加权汇总（0-100）。

    若提供 weights_override，则用覆盖权重替代模板默认权重（J1 校准生效点）。
    缺失/异常维度直接校验失败；不对不完整输出重新归一化。
    """
    rule_ids = [rule["id"] for rule in rules]
    scores = _validate_dimension_scores(dimension_scores, rule_ids)
    weights = {
        rule["id"]: (weights_override or {}).get(rule["id"], float(rule.get("weight", 0.0)))
        for rule in rules
    }
    if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
        raise QualityCheckError("质检权重必须是非负有限数")
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise QualityCheckError("质检权重之和必须大于零")
    return sum(scores[rid] * weight for rid, weight in weights.items()) / total_weight


def _validate_dimension_scores(data: object, rule_ids: List[str]) -> Dict[str, float]:
    """动态 Pydantic Schema：维度必须齐全、唯一，严格数值且范围为 0–100。"""
    if not rule_ids or len(set(rule_ids)) != len(rule_ids):
        raise QualityCheckError("质检规则必须包含非空且唯一的维度 id")
    score_type = confloat(strict=True, ge=0, le=100, allow_inf_nan=False)
    # 动态 Schema 边界；通过 alias 支持任意题型声明的维度 id，而非 Python 字段名限制。
    fields: dict[str, Any] = {
        f"dimension_{index}": (score_type, Field(..., alias=rid))
        for index, rid in enumerate(rule_ids)
    }
    model = create_model("JudgeDimensionScores", __config__=ConfigDict(extra="forbid"), **fields)
    return {
        rid: float(value)
        for rid, value in model.model_validate(data).model_dump(by_alias=True).items()
    }


def aggregate_rounds(round_scores: List[Dict[str, float]]) -> Dict[str, float]:
    """将多轮 judge 采样按维度取均值，降低单次抽样的抖动。

    所有轮次必须有相同且完整的维度；每个维度求算术平均。
    均值聚合使同一样本的质检分趋于稳定（方差按轮数 n 下降为单次的 1/n）。
    """
    if not round_scores:
        raise QualityCheckError("质检采样不能为空")
    expected = list(round_scores[0])
    round_scores = [_validate_dimension_scores(once, expected) for once in round_scores]
    accumulated: Dict[str, List[float]] = {}
    for once in round_scores:
        for rid, score in once.items():
            accumulated.setdefault(rid, []).append(score)
    return {rid: mean(scores) for rid, scores in accumulated.items()}


def _get_openai_client() -> OpenAI:
    """构建 OpenAI 兼容客户端（可测试缝：单测/集成测试可整体替换本函数）。"""
    return OpenAI(
        base_url=settings.JUDGE_API_BASE or settings.LLM_API_BASE,
        api_key=settings.JUDGE_API_KEY or settings.LLM_API_KEY or "sk-placeholder",
        timeout=settings.LLM_TIMEOUT,
    )


def _build_judge_prompt(
    template: Any, payload: Dict[str, Any], generation_params: Optional[Dict[str, Any]] = None
) -> Tuple[str, str]:
    """构造 judge 的系统与用户提示。

    - system/user prompt 从独立 .st 文件加载（prompts/judge-*.st），符合
      Prompt 工程化规范，可独立版本化与调试；
    - 注入 judge 质检技能规范（skills/judge/SKILL.md）作为打分行为约束；
    - 将模板 quality_rules 的维度描述与分档评分标准（rubric）结构化注入
      user prompt，让 judge 依据统一、明确的评分尺度打分，降低尺度漂移；
    - 兼容旧模板：仅含 id/weight 时退化为简单打分说明。
    """
    rules = template.quality_rules or []
    rubric_lines = []
    for r in rules:
        rid = r.get("id", "")
        weight = r.get("weight", 0.0)
        desc = r.get("description", "")
        rubric = r.get("rubric") or {}
        base = f"- {rid}（权重 {weight}）"
        if desc:
            base += f"：{desc}"
        rubric_lines.append(base)
        if isinstance(rubric, dict):
            for band, std in rubric.items():
                rubric_lines.append(f"    {band} 分：{std}")

    # 从独立 .st 文件加载 system prompt，并注入 judge 质检技能规范
    system_prompt = load_prompt("judge-system.st")
    skill_md = get_skill_by_id("judge")
    if skill_md:
        system_prompt = f"{system_prompt}\n\n# 质检技能规范\n{skill_md}"

    # 仅传模板声明的生成约束；RAG、凭据和改版上下文不能混入评分目标。
    properties = (getattr(template, "input_schema", None) or {}).get("properties", {})
    constraints = {
        key: value for key, value in (generation_params or {}).items() if key in properties
    }
    # 渲染 user prompt：分别注入生成约束、rubric 与待质检 payload
    user_prompt = render(
        load_prompt("judge-user.st"),
        {
            "rubric": "\n".join(rubric_lines),
            "generation_constraints": json.dumps(constraints, ensure_ascii=False),
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )
    return system_prompt, user_prompt


def _call_judge_once(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    rule_ids: List[str],
) -> Tuple[str, float, Any]:
    """调用 Judge 一次，先返回原文以保留无效输出的 usage 和耗时。"""
    start = time.perf_counter_ns()
    resp = client.chat.completions.create(
        model=model,
        temperature=settings.JUDGE_TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    return content, elapsed_ms(start), resp.usage


def run_quality_check(
    template: Any,
    payload: Dict[str, Any],
    model_name: str = "",
    trace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    template_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    rounds: Optional[int] = None,
    weights_override: Optional[Dict[str, float]] = None,
    generation_params: Optional[Dict[str, Any]] = None,
    model_profile: ModelProfile | None = None,
) -> Tuple[float, Dict[str, Any]]:
    """执行自动质检，返回 (总分 0-100, 维度分 dict)。

    - 按 quality_rules 逐维度打 0-100 分；
    - 多次采样（默认取配置 JUDGE_SAMPLE_ROUNDS，或显式 rounds）取各维度均值，降低抖动；
    - 按权重加权汇总得到总分（若提供 weights_override 则用校准权重）；
    - judge 模型：显式传入优先；否则 JUDGE_MODEL_NAME / LLM_MODEL_NAME 兜底（P1-2）；
    - 每次 judge 采样记录 TraceLog（耗时/成本）与结构化日志。
    """
    trace_id = trace_id or f"qc:{uuid.uuid4()}"
    if not model_name:
        model_name = settings.JUDGE_MODEL_NAME or settings.LLM_MODEL_NAME
    rules = template.quality_rules or []
    rule_ids: List[str] = []
    for rule in rules:
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid:
            raise QualityCheckError("质检规则必须声明非空字符串维度 id")
        rule_ids.append(rid)

    try:
        configured = model_profile
        if configured is not None:
            model_name = configured.model_name
        client = (
            client_for_profile(configured)
            if configured is not None and configured.provider_id
            else _get_openai_client()
        )
    except ProviderConfigurationError as exc:
        raise QualityCheckError(str(exc)) from exc
    system_prompt, user_prompt = _build_judge_prompt(template, payload, generation_params)

    judge_rounds = rounds if rounds is not None else settings.JUDGE_SAMPLE_ROUNDS
    max_attempts = int(
        (getattr(template, "run_config", None) or {}).get(
            "judge_max_retry", settings.JUDGE_MAX_RETRIES
        )
    )
    if judge_rounds < 1 or max_attempts < 1:
        raise QualityCheckError("质检采样轮数和最大尝试次数必须至少为 1")
    constraints = {
        key: value
        for key, value in (generation_params or {}).items()
        if key in (getattr(template, "input_schema", None) or {}).get("properties", {})
    }
    round_scores: List[Dict[str, float]] = []
    for round_idx in range(judge_rounds):
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter_ns()
            text = None
            usage = None
            latency_ms = 0.0
            once = None
            try:
                text, latency_ms, usage = _call_judge_once(
                    client, model_name, system_prompt, user_prompt, rule_ids
                )
                data = json.loads(text)
                once = _validate_dimension_scores(data.get("dimension_scores"), rule_ids)
            except (
                ValidationError,
                json.JSONDecodeError,
                AttributeError,
                QualityCheckError,
                OpenAIError,
            ) as exc:
                last_error = (
                    QualityCheckError(f"Provider质检响应失败: {type(exc).__name__}")
                    if configured is not None and isinstance(exc, OpenAIError)
                    else exc
                )
                latency_ms = latency_ms or elapsed_ms(started)
                logger.warning(
                    "质检无效 trace_id=%s round=%d attempt=%d: %s",
                    trace_id,
                    round_idx + 1,
                    attempt,
                    last_error,
                )
            tokens = token_breakdown(usage, model_name)
            if trace_id:
                record_trace(
                    snapshot_data={
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "response_text": text,
                        "model": model_name,
                        "temperature": settings.JUDGE_TEMPERATURE,
                        "response_format": {"type": "json_object"},
                    },
                    trace_id=trace_id,
                    task_id=task_id,
                    template_id=template_id,
                    model=model_name,
                    latency_ms=latency_ms,
                    cost=compute_cost(usage, model_name),
                    prompt_version=template.version,
                    tenant_id=tenant_id,
                    input_data={
                        "payload": payload,
                        "model_profile": profile_identity(configured),
                        "generation_constraints": constraints,
                        "sample_round": round_idx + 1,
                    },
                    output_data=(
                        {"dimension_scores": once}
                        if once is not None
                        else {"error_summary": str(last_error)[:2000]}
                    ),
                    stage="qc",
                    prompt_tokens=tokens["prompt_tokens"],
                    completion_tokens=tokens["completion_tokens"],
                    usage_reported=usage_is_reported(usage, "qc"),
                    prompt_cost=tokens["prompt_cost"],
                    completion_cost=tokens["completion_cost"],
                    attempt=attempt,
                    success=once is not None,
                )
            if once is not None:
                round_scores.append(once)
                break
        else:
            raise QualityCheckError(
                f"自动质检第 {round_idx + 1} 轮耗尽 {max_attempts} 次尝试：{last_error}"
            ) from last_error
    dimension_scores = aggregate_rounds(round_scores)
    score = _aggregate_score(rules, dimension_scores, weights_override)
    return round(score, 2), dimension_scores
