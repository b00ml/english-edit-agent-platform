# app/engine/trace.py —— LLM 调用可观测性与成本记录
# 每次 LLM 调用组装一条 trace dict，按 settings.TRACE_SINKS 分发到各 Sink：
#   - db：自研 TraceLog 表（SSOT，trace_id/model/cost/latency/attempt/success 入库），
#     供成本聚合接口（/api/costs）与链路回放使用；
#   - langfuse：可选导出通道（见 observability.LangfuseSink）。
# 任一 Sink 写入失败仅告警，不影响其余 Sink 与业务主流程。
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from app.config import settings
from app.engine.trace_recovery import note_durability, note_sink, spool_trace
from app.errors import TracePersistenceError

logger = logging.getLogger("app.engine.trace")

# Sink 单例缓存（按 sink 名），reset_sinks() 供配置变更与测试后重建
_SINKS: Dict[str, Any] = {}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)([^\s,;]+)")
_QUOTED_SECRET_PATTERN = re.compile(
    r'("(?:[\w-]+_)?(?:api[_-]?key|authorization|password|secret|'
    r'access[_-]?token|refresh[_-]?token|token)"\s*:\s*)'
    r'("(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否包含凭据，兼容 llm_api_key/jwt_secret 等组合命名。"""
    normalized = key.lower().replace("-", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return any(
        normalized.endswith(suffix)
        for suffix in (
            "_api_key",
            "_authorization",
            "_password",
            "_secret",
            "_access_token",
            "_refresh_token",
            "_token",
        )
    )


def _sanitize_string(value: str, max_string_length: int) -> str:
    """脱敏字符串值中的常见 key=value 和 Bearer 凭据。"""
    # 先处理 Bearer，避免 ``Authorization: Bearer <token>`` 被 key=value
    # 规则截断为只脱敏 ``Bearer``，从而把真正凭据留在字符串中。
    value = _QUOTED_SECRET_PATTERN.sub(lambda match: match.group(1) + '"[REDACTED]"', value)
    sanitized = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    sanitized = _SENSITIVE_VALUE_PATTERN.sub(r"\1\2[REDACTED]", sanitized)
    if len(sanitized) > max_string_length:
        return f"{sanitized[:max_string_length]}...[truncated]"
    return sanitized


def sanitize_trace_data(value: Any, *, max_string_length: int = 2000) -> Any:
    """递归脱敏 Trace 输入/输出，防止密钥、凭据和完整大文本进入存储。"""
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            sanitized[key_text] = (
                "[REDACTED]"
                if _is_sensitive_key(key_text)
                else sanitize_trace_data(item, max_string_length=max_string_length)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_data(item, max_string_length=max_string_length) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, max_string_length)
    return value


def reset_sinks() -> None:
    """清空 Sink 缓存，下次 record_trace 按最新配置重建。"""
    _SINKS.clear()


def _build_sinks() -> None:
    """按 TRACE_SINKS 配置实例化 Sink（db / langfuse）。"""
    from app.engine.observability import LangfuseSink, TraceLogSink

    for name in (s.strip() for s in settings.TRACE_SINKS.split(",") if s.strip()):
        if name in _SINKS:
            continue
        if name == "db":
            _SINKS[name] = TraceLogSink()
        elif name == "langfuse":
            _SINKS[name] = LangfuseSink()
        else:
            logger.warning("未知 trace sink 配置项: %s（已跳过）", name)


def _get_sinks() -> List[Any]:
    if not _SINKS:
        _build_sinks()
    return list(_SINKS.values())


def _model_rates(model: str) -> tuple[float, float]:
    """解析模型单价（每 1K token）：命中 MODEL_PRICES 用分模型价，未命中回退全局单一单价。

    价目表形如 {"模型名": {"prompt": x, "completion": y}}；缺某侧单价时该侧回退全局价。
    """
    price = (settings.MODEL_PRICES or {}).get(model)
    if price:
        flat = settings.COST_PER_1K_TOKENS
        return float(price.get("prompt", flat)), float(price.get("completion", flat))
    rate = settings.COST_PER_1K_TOKENS
    return rate, rate


def compute_cost(usage: Optional[Any], model: str) -> float:
    """按 token 用量估算成本：优先分模型价目表（P1-3），未命中回退全局单价。"""
    prompt, completion = _token_usage(usage)
    p_rate, c_rate = _model_rates(model)
    return round(prompt / 1000.0 * p_rate + completion / 1000.0 * c_rate, 6)


def token_breakdown(usage: Optional[Any], model: str) -> Dict[str, Any]:
    """将 token 用量细分为输入/输出 token 数与各自成本（分模型计价）。

    供深度成本报表（K2）按生成/质检阶段拆分 token 与成本。
    返回 {prompt_tokens, completion_tokens, prompt_cost, completion_cost, cost}。
    """
    prompt, completion = _token_usage(usage)
    p_rate, c_rate = _model_rates(model)
    prompt_cost = round(prompt / 1000.0 * p_rate, 6)
    completion_cost = round(completion / 1000.0 * c_rate, 6)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_cost": prompt_cost,
        "completion_cost": completion_cost,
        "cost": round(prompt_cost + completion_cost, 6),
    }


def usage_is_reported(usage: object, stage: str) -> bool:
    fields = ("prompt_tokens",) if stage == "embedding" else ("prompt_tokens", "completion_tokens")
    return all(
        isinstance(getattr(usage, name, None), int)
        and not isinstance(getattr(usage, name, None), bool)
        and getattr(usage, name) >= 0
        for name in fields
    )


def _token_usage(usage: Optional[Any]) -> tuple[int, int]:
    """提取 usage 中的输入/输出 token 数（缺失按 0 计）。"""
    if usage is None:
        return 0, 0
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion = getattr(usage, "completion_tokens", None) or 0
    return int(prompt), int(completion)


def record_trace(
    *,
    trace_id: str,
    model: str,
    latency_ms: float,
    cost: float,
    prompt_version: str = "",
    task_id: Optional[str] = None,
    template_id: Optional[str] = None,
    item_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    input_data: Optional[Dict[str, Any]] = None,
    output_data: Optional[Dict[str, Any]] = None,
    stage: str = "",
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    attempt: int = 1,
    success: bool = True,
    usage_reported: bool | None = None,
    prompt_cost: float | None = None,
    completion_cost: float | None = None,
    snapshot_data: Dict[str, Any] | None = None,
) -> None:
    """组装一条 trace 并分发到所有配置的 Sink（失败尝试同样记录）。

    attempt/success 用于结构化符合率统计（P0-2）：generate 阶段每次调用
    （含校验失败的尝试）各写一行，失败行 output_data 为 {"error_summary": ...}。
    单个 Sink 失败仅告警，不中断其余 Sink 与生成主流程。
    """
    from app.engine.observability import TraceLogSink
    from app.engine.trace_snapshots import prepare_snapshot

    prepared = prepare_snapshot(snapshot_data)
    trace: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "snapshot_status": prepared.status,
        "usage_reported": usage_reported,
        "prompt_cost": prompt_cost,
        "completion_cost": completion_cost,
        "trace_id": trace_id,
        "task_id": task_id,
        "template_id": template_id,
        "item_id": item_id,
        "prompt_version": prompt_version,
        "model": model,
        "input_data": sanitize_trace_data(input_data),
        "output_data": sanitize_trace_data(output_data),
        "latency_ms": latency_ms,
        "cost": cost,
        "stage": stage,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tenant_id": tenant_id,
        "attempt": attempt,
        "success": success,
    }
    database_written = False
    for sink in _get_sinks():
        name = getattr(sink, "name", type(sink).__name__)
        try:
            if isinstance(sink, TraceLogSink) and hasattr(sink, "emit_with_snapshot"):
                sink.emit_with_snapshot(trace, prepared)
            else:
                sink.emit(trace)
            note_sink(name, True)
            database_written = database_written or name == "db"
        except (
            Exception
        ) as exc:  # noqa: BLE001 - preserve other sinks, never log raw credentials/SQL values
            note_sink(name, False, type(exc).__name__)
            logger.warning("trace sink 写入失败 sink=%s error_type=%s", name, type(exc).__name__)
    if not database_written and trace["snapshot_status"] == "available":
        trace["snapshot_status"] = "capture_failed"
    durable = database_written
    if not durable and settings.TRACE_FAILURE_SPOOL_DIR:
        try:
            durable = spool_trace(trace)
        except OSError as exc:
            note_sink("spool", False, type(exc).__name__)
            logger.error("trace spool 写入失败 error_type=%s", type(exc).__name__)
    note_durability(durable)
    if not durable and settings.TRACE_REQUIRE_DURABILITY:
        raise TracePersistenceError("Trace 无法写入数据库或持久化补偿目录，拒绝静默丢失")


def record_lifecycle_event(
    *,
    trace_id: str,
    stage: str,
    event: str,
    model: str,
    task_id: Optional[str] = None,
    template_id: Optional[str] = None,
    item_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    status: Optional[str] = None,
    reason: Optional[str] = None,
    success: bool = True,
    latency_ms: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """记录队列/工作流生命周期事件，复用 TraceLog 作为统一回放事实源。

    生命周期事件不包含模型输入输出，因此成本固定为 0；业务上下文放在
    input/output 的结构化字段中，仍经过 ``sanitize_trace_data`` 处理。
    """
    details: Dict[str, Any] = {"event": event}
    if metadata:
        details.update(metadata)
    output: Dict[str, Any] = {"event": event}
    if status is not None:
        output["status"] = status
    if reason:
        output["reason"] = reason
    record_trace(
        trace_id=trace_id,
        model=model,
        latency_ms=latency_ms,
        cost=0.0,
        task_id=task_id,
        template_id=template_id,
        item_id=item_id,
        tenant_id=tenant_id,
        input_data=details,
        output_data=output,
        stage=stage,
        success=success,
    )


def elapsed_ms(start_ns: int) -> float:
    """从单调时钟起点计算耗时（毫秒）。"""
    return round((time.perf_counter_ns() - start_ns) / 1_000_000, 2)
