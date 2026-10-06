# app/workflow/graph.py —— LangGraph 生成状态机
# 拓扑：生成 -> 校验 -> 质检 -> (改版 或 入库)。
# checkpointer 默认 PostgresSaver（持久化断点，跨重启续跑；thread_id=task:idx 粒度），
# 初始化失败自动降级 MemorySaver；对已完成 thread 重复 invoke 直接返回落库终态，
# 因此配合任务重投/恢复即得"已完成条目不重复生成"的幂等续跑语义。
import hashlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.calibration import get_effective_weights
from app.config import settings
from app.database import SessionLocal
from app.domain.status import item_status_from_graph
from app.engine.content_validation import enforce_content, inspect_content
from app.engine.fewshot import select_fewshot
from app.engine.providers import judge_profile_for_template
from app.engine.quality import resolve_judge_model, run_quality_check
from app.engine.router import generate_with_fallback
from app.engine.trace import elapsed_ms, record_lifecycle_event
from app.errors import (
    CheckpointerUnavailableError,
    ContentStateConflictError,
    ContentValidationError,
    QualityCheckError,
    TemplateNotFoundError,
)
from app.models import ContentItem, QualityEvaluation, QualityRecord, QuestionTemplate
from app.notification import notify_content_rejected, notify_human_review
from app.prompt_loader import load_prompt, render
from app.rag.provenance import reference_snapshot
from app.rag.retriever import build_rag_context
from app.versioning import quality_snapshot

logger = logging.getLogger("app.workflow.graph")

# 默认改版上限
_DEFAULT_MAX_REVISE = 3
# 人工质检默认阈值（取自全局配置，便于调优）
_DEFAULT_QUALITY_THRESHOLD = settings.QUALITY_THRESHOLD


class GenState(TypedDict, total=False):
    """状态机共享状态。"""

    task_id: str
    params: dict
    draft: Optional[dict]
    qc_score: float
    dimension_scores: dict
    revise_count: int
    trace_id: str
    status: str
    content_id: Optional[str]
    # 以下三个键由 run_generation 从模板 run_config 注入 initial_state。
    # 必须显式声明（审查 P1 修复）：langgraph 1.2.x 对未声明键宽松传播，但这是
    # 未定义行为——升级收紧 schema 会让灰区卡点与模板阈值静默失效且测试全绿。
    max_revise: int
    quality_threshold: float
    human_review_config: dict
    quality_config_snapshot: dict
    rag_provenance: dict
    fewshot_report: dict
    validation_report: dict
    failure_code: str
    failure_reason: str


# 全局 checkpointer 单例（惰性初始化）：backend 与 worker 各进程首次调用时构建
_checkpointer: Optional[BaseCheckpointSaver] = None
_checkpointer_lock = threading.Lock()
_checkpointer_connection = None
_checkpointer_pid = os.getpid()
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _require_persistent(saver: BaseCheckpointSaver) -> BaseCheckpointSaver:
    if (
        isinstance(saver, MemorySaver)
        and settings.ENVIRONMENT in {"staging", "production"}
        and not settings.ALLOW_MEMORY_CHECKPOINTER
    ):
        raise CheckpointerUnavailableError(
            "生产/预发布环境必须使用持久化断点，MemorySaver 降级被禁止"
        )
    return saver


@contextmanager
def _thread_execution_lock(session: Session, thread_id: str) -> Iterator[None]:
    """同一 thread 串行执行：Postgres advisory lock 跨进程，内存模式仅进程内。"""
    bind = session.get_bind() if isinstance(session, Session) else None
    if bind is not None and bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(thread_id.encode()).digest()[:8], "big", signed=True)
        # 独立连接持有会话锁，不受节点事务 commit 影响。
        with bind.engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                conn.commit()
    else:
        with _thread_locks_guard:
            lock = _thread_locks.setdefault(thread_id, threading.RLock())
        with lock:
            yield


def _get_checkpointer() -> BaseCheckpointSaver:
    """按 CHECKPOINTER_BACKEND 构建 checkpointer 单例。

    - postgres：PostgresSaver（psycopg3 连接，autocommit），setup() 幂等建表；
    - memory：进程内 MemorySaver（旧行为）；
    - postgres 初始化失败（包缺失/连不上）时告警并降级 memory，可用性优先。
    """
    global _checkpointer, _checkpointer_connection, _checkpointer_pid
    if _checkpointer_pid != os.getpid():
        _checkpointer = None
        _checkpointer_connection = None
        _checkpointer_pid = os.getpid()
    if _checkpointer is not None:
        return _require_persistent(_checkpointer)
    with _checkpointer_lock:
        if _checkpointer is not None:
            return _require_persistent(_checkpointer)
        if settings.CHECKPOINTER_BACKEND == "postgres":
            try:
                import psycopg
                from langgraph.checkpoint.postgres import PostgresSaver
                from psycopg.rows import dict_row

                # SQLAlchemy 串转 psycopg3 DSN；参数与 langgraph from_conn_string 一致。
                # connect_timeout 快速失败：DB 短暂不可用时尽快降级 memory，不拖慢启动
                dsn = settings.DATABASE_URL.replace("+psycopg2", "")
                conn = psycopg.connect(
                    dsn,
                    autocommit=True,
                    prepare_threshold=0,
                    connect_timeout=5,
                    row_factory=dict_row,
                )
                _checkpointer_connection = conn
                saver = PostgresSaver(conn)
                saver.setup()  # 建表 + 迁移，幂等
                _checkpointer = saver
                logger.info("checkpointer 已就绪: PostgresSaver（断点持久化）")
                return _checkpointer
            except Exception as exc:  # noqa: BLE001 —— 持久化初始化失败降级，保证可用性
                if _checkpointer_connection is not None:
                    _checkpointer_connection.close()
                    _checkpointer_connection = None
                logger.warning("PostgresSaver 初始化失败: %s", exc)
        else:
            logger.info("checkpointer: MemorySaver（CHECKPOINTER_BACKEND=memory）")
        _checkpointer = MemorySaver()
        return _require_persistent(_checkpointer)


def reset_checkpointer() -> None:
    """关闭拥有的数据库连接并重置单例，供配置变更与单测隔离使用。"""
    global _checkpointer, _checkpointer_connection
    with _checkpointer_lock:
        if _checkpointer_connection is not None:
            _checkpointer_connection.close()
        _checkpointer_connection = None
        _checkpointer = None


def _load_template(session: Session, template_id: str) -> QuestionTemplate:
    """按 type_id 加载题型模板。"""
    template = (
        session.query(QuestionTemplate).filter(QuestionTemplate.type_id == template_id).first()
    )
    if template is None:
        raise TemplateNotFoundError(f"题型模板不存在: {template_id}")
    return template


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------
def generate_node(state: GenState, session: Session) -> dict:
    """生成节点：调用引擎生成草稿（含主->备->默认降级）。

    生成前按知识点做 RAG 检索，将命中的知识片段注入提示作为事实依据；
    若 params 携带改版上下文（revise_context，由 revise 节点准备），则将
    上一稿与质检反馈注入 user prompt，使改版有据可依。
    """
    template = _load_template(session, state["params"]["template_id"])
    profile_name = (template.run_config or {}).get("model_profile")

    # RAG 检索注入：以知识点为查询，命中则附加到参数上下文
    params = dict(state["params"])
    params.pop("fewshot_context", None)
    params.pop("fewshot_provenance", None)
    # Also normalize legacy task/checkpoint params before rendering a single-item prompt.
    if "quantity" in params:
        params["quantity"] = 1
    knowledge_point = params.get("knowledge_point") or ""
    rag_config = (template.run_config or {}).get("rag", {})
    rag_provenance = {
        "status": "disabled" if rag_config.get("mode", settings.RAG_MODE) == "off" else "no_query",
        "mode": rag_config.get("mode", settings.RAG_MODE),
        "citations": [],
        "verification": "unverified",
        "require_review": bool(rag_config.get("require_human_verification", False)),
    }
    if (
        knowledge_point
        or rag_config.get("mode", settings.RAG_MODE) == "required"
        or rag_provenance["require_review"]
    ):
        rag_context = build_rag_context(
            session,
            query=knowledge_point,
            knowledge_point=knowledge_point,
            tenant_id=params.get("tenant_id"),
            trace_id=state["trace_id"],
            task_id=state.get("task_id"),
            template_id=template.type_id,
            provenance=rag_provenance,
            scope_mode=rag_config.get("scope_mode"),
            context_mode=rag_config.get("context_mode", settings.RAG_CONTEXT_MODE),
            mode=(
                "required"
                if rag_provenance["require_review"]
                else rag_config.get("mode", settings.RAG_MODE)
            ),
        )
        if rag_context:
            params["rag_context"] = rag_context

    revise_context = params.get("revise_context")
    if revise_context:
        # 改版注入：上一稿 + 质检反馈（Prompt 文本独立维护于 revise-instruction.st），
        # 由 generate_structured 从 params 读取并并入 user prompt
        params["revise_instruction"] = render(
            load_prompt("revise-instruction.st"),
            {
                "qc_score": revise_context.get("qc_score"),
                "quality_threshold": state.get("quality_threshold"),
                "dimension_scores": json.dumps(
                    {
                        **(revise_context.get("dimension_scores") or {}),
                        "deterministic_errors": revise_context.get("validation_errors", []),
                    },
                    ensure_ascii=False,
                ),
                "previous_draft": json.dumps(
                    revise_context.get("previous_draft") or {}, ensure_ascii=False
                ),
            },
        )

    fewshot_report = select_fewshot(session, template, params, task_id=state.get("task_id"))
    if fewshot_report["context"]:
        params["fewshot_context"] = fewshot_report["context"]
    params["fewshot_provenance"] = {k: v for k, v in fewshot_report.items() if k != "context"}

    draft = generate_with_fallback(
        template,
        params,
        session,
        profile_name,
        trace_id=state["trace_id"],
        task_id=state.get("task_id"),
        template_id=template.type_id,
        tenant_id=params.get("tenant_id"),
    )
    return {
        "draft": draft,
        "rag_provenance": rag_provenance,
        "fewshot_report": {k: v for k, v in fewshot_report.items() if k != "context"},
        "status": "generated",
    }


def validate_node(state: GenState, session: Session) -> dict:
    """校验节点：对草稿做基础结构校验（schema 已在引擎二次校验，此处为兜底）。"""
    draft = state.get("draft") or {}
    template = _load_template(session, state["params"]["template_id"])
    report = inspect_content(draft, template.output_schema, template.run_config)
    if report.valid:
        return {
            "status": "validated",
            "validation_report": report.model_dump(),
            "failure_code": "",
            "failure_reason": "",
        }
    return {
        "status": "invalid",
        "validation_report": report.model_dump(),
        "failure_code": "deterministic_validation_failed",
        "failure_reason": "; ".join(e.message for e in report.errors),
    }


def qc_node(state: GenState, session: Session) -> dict:
    """质检节点：LLM-as-judge 打分。质检记录在入库后绑定内容条目标记。

    若存在校准覆盖权重（J1 质量闭环），则用校准权重替代模板默认权重汇总。
    judge 模型按 模板 run_config.judge_model > 全局 JUDGE_MODEL_NAME > 生成默认 解析
    （P1-2 自偏好治理：建议 judge 与生成主模型不同家族）。
    """
    template = _load_template(session, state["params"]["template_id"])
    draft = state.get("draft") or {}
    report = enforce_content(draft, template.output_schema, template.run_config)
    # 读取校准覆盖权重（无校准则返回 None，回退模板默认）
    weights_override, calib_threshold = get_effective_weights(
        session, template.type_id, tenant_id=state["params"].get("tenant_id")
    )
    revision = state.get("revise_count", 0)
    event = (
        session.query(QualityEvaluation)
        .filter(
            QualityEvaluation.thread_id == state["trace_id"],
            QualityEvaluation.revise_count == revision,
        )
        .first()
    )
    if event is not None and event.status == "completed":
        return {
            "qc_score": event.score,
            "dimension_scores": event.dimension_scores,
            "quality_config_snapshot": event.config_snapshot,
            "quality_threshold": event.threshold,
            "status": "qc_done",
        }
    threshold = float(
        calib_threshold
        if calib_threshold is not None
        else (template.run_config or {}).get("quality_threshold", _DEFAULT_QUALITY_THRESHOLD)
    )
    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise QualityCheckError("生效质检阈值必须是 0–100 的有限数")
    snapshot = quality_snapshot(
        template,
        model_name=resolve_judge_model(template, session, state["params"].get("tenant_id")),
        threshold=threshold,
        weights=weights_override,
    )
    configured_judge = (
        judge_profile_for_template(session, template.type_id, state["params"].get("tenant_id"))
        if isinstance(session, Session)
        else None
    )
    if configured_judge is not None:
        snapshot["judge_profile"] = {
            "name": configured_judge.name,
            "model": configured_judge.model_name,
            "model_hash": configured_judge.model_hash,
            "provider_id": configured_judge.provider_id,
            "provider_config_hash": configured_judge.provider_config_hash,
        }
    snapshot["content_validation"] = report.model_dump()
    snapshot["fewshot"] = state.get("fewshot_report", {})
    snapshot["rag_provenance"] = state.get("rag_provenance", {})
    snapshot["generation_constraints"] = {
        key: value
        for key, value in state["params"].items()
        if key in (getattr(template, "input_schema", None) or {}).get("properties", {})
    }
    try:
        judge_profile = (
            judge_profile_for_template(session, template.type_id, state["params"].get("tenant_id"))
            if isinstance(session, Session)
            else None
        )
        score, dimension_scores = run_quality_check(
            template,
            draft,
            resolve_judge_model(template, session, state["params"].get("tenant_id")),
            trace_id=state["trace_id"],
            task_id=state.get("task_id"),
            template_id=template.type_id,
            tenant_id=state["params"].get("tenant_id"),
            weights_override=weights_override,
            generation_params=state["params"],
            model_profile=judge_profile,
        )
    except QualityCheckError as exc:
        _save_quality_evaluation(session, event, state, snapshot, threshold, None, {}, str(exc))
        raise
    _save_quality_evaluation(
        session, event, state, snapshot, threshold, score, dimension_scores, None
    )
    return {
        "qc_score": score,
        "dimension_scores": dimension_scores,
        "quality_config_snapshot": snapshot,
        "quality_threshold": threshold,
        "status": "qc_done",
    }


def _save_quality_evaluation(
    session: Session,
    event: QualityEvaluation | None,
    state: GenState,
    snapshot: dict,
    threshold: float,
    score: float | None,
    dims: dict,
    failure_reason: str | None,
) -> None:
    """每个 thread/改版轮次只写一个评分结果，底层采样尝试仍保留于 Trace。"""
    if event is None:
        event = QualityEvaluation(
            task_id=state.get("task_id"),
            template_id=state["params"]["template_id"],
            thread_id=state["trace_id"],
            revise_count=state.get("revise_count", 0),
            tenant_id=state["params"].get("tenant_id"),
        )
        session.add(event)
    event.status = "failed" if failure_reason is not None else "completed"
    event.is_final = failure_reason is not None
    event.score = score
    event.dimension_scores = dims
    event.threshold = threshold
    event.config_snapshot = snapshot
    event.failure_reason = failure_reason
    session.commit()


def _finalize_quality_evaluation(session: Session, state: GenState) -> None:
    event = (
        session.query(QualityEvaluation)
        .filter(
            QualityEvaluation.thread_id == state["trace_id"],
            QualityEvaluation.revise_count == state.get("revise_count", 0),
        )
        .first()
    )
    if event is not None:
        event.is_final = True


def revise_node(state: GenState, session: Session) -> dict:
    """改版节点：准备改版上下文（上一稿 + 质检反馈），由下游 generate 节点统一生成。

    修复（OPT-023）：此前本节点自行调用一次生成、随后经 revise->generate 无条件边
    generate 节点又再生成一次——每次改版双倍 LLM 调用，且改版草稿被无条件覆盖、
    质检反馈从未进入生成 prompt。现改为只准备 revise_context，生成统一收敛到
    generate 节点（每次改版 1 次 LLM 调用，且反馈真实注入）。
    """
    params = dict(state["params"])
    params["revise_context"] = {
        "previous_draft": state.get("draft") or {},
        "qc_score": state.get("qc_score"),
        "dimension_scores": state.get("dimension_scores") or {},
        "validation_errors": (state.get("validation_report") or {}).get("errors", []),
    }
    return {
        "params": params,
        "revise_count": state.get("revise_count", 0) + 1,
        "status": "revised",
    }


def store_node(state: GenState, session: Session) -> dict:
    """入库节点：将通过的草稿写入 ContentItem，并绑定本次质检记录。"""
    template = _load_template(session, state["params"]["template_id"])
    existing = session.query(ContentItem).filter(ContentItem.thread_id == state["trace_id"]).first()
    if existing is not None:
        return {"content_id": existing.id, "status": "stored"}
    report = enforce_content(state.get("draft") or {}, template.output_schema, template.run_config)
    item = ContentItem(
        validation_report=report.model_dump(),
        provenance=state.get("rag_provenance"),
        tenant_id=state["params"].get("tenant_id"),
        task_id=state["task_id"],
        template_id=state["params"]["template_id"],
        payload=state["draft"] or {},
        qc_score=state.get("qc_score"),
        revise_count=state.get("revise_count", 0),
        status="pending_qc",
        thread_id=state["trace_id"],
    )
    session.add(item)
    session.flush()  # 先拿到 item.id

    # 自动质检记录与内容条目绑定
    record = QualityRecord(
        tenant_id=state["params"].get("tenant_id"),
        item_id=item.id,
        score=state.get("qc_score", 0.0),
        dimension_scores=state.get("dimension_scores", {}),
        source="auto",
        template_version=getattr(template, "version", 1),
        config_snapshot=state.get("quality_config_snapshot"),
    )
    session.add(record)
    _finalize_quality_evaluation(session, state)
    session.commit()
    return {"content_id": item.id, "status": "stored"}


def reject_node(state: GenState, session: Session) -> dict:
    """拒绝节点：改版次数耗尽且未达标，标记失败，落库 failure_code/failure_reason。"""
    failure_code = state.get("failure_code") or "quality_threshold_not_met"
    failure_reason = state.get("failure_reason") or (
        f"质检分数 {state.get('qc_score', 0.0)} 低于阈值，已重试 {state.get('revise_count', 0)} 次"
    )

    existing = session.query(ContentItem).filter(ContentItem.thread_id == state["trace_id"]).first()
    if existing is not None:
        return {"content_id": existing.id, "status": "rejected"}
    item = ContentItem(
        validation_report=state.get("validation_report"),
        provenance=state.get("rag_provenance"),
        tenant_id=state["params"].get("tenant_id"),
        task_id=state["task_id"],
        template_id=state["params"]["template_id"],
        payload=state.get("draft") or {},
        qc_score=state.get("qc_score"),
        revise_count=state.get("revise_count", 0),
        status="rejected",
        thread_id=state["trace_id"],
        failure_code=failure_code,
        failure_reason=failure_reason,
    )
    session.add(item)
    session.flush()
    template = _load_template(session, state["params"]["template_id"])
    session.add(
        QualityRecord(
            item_id=item.id,
            score=state.get("qc_score", 0.0),
            dimension_scores=state.get("dimension_scores", {}),
            source="auto",
            template_version=getattr(template, "version", 1),
            config_snapshot=state.get("quality_config_snapshot"),
            tenant_id=state["params"].get("tenant_id"),
        )
    )
    _finalize_quality_evaluation(session, state)
    session.commit()
    return {"content_id": item.id, "status": "rejected", "failure_code": failure_code}


def submit_review_node(state: GenState, session: Session) -> dict:
    """灰区转人工（P1-1）：草稿落库为 awaiting_review 条目，写 auto 质检记录并通知。

    与 interrupt 分离为独立节点：本节点正常返回后状态（content_id）即写入检查点，
    下一个节点（human_review）内 interrupt 暂停不会导致本节点副作用重复执行。
    thread_id 存入条目，供 review API 凭 Command(resume) 恢复图。
    """
    template = _load_template(session, state["params"]["template_id"])
    existing = session.query(ContentItem).filter(ContentItem.thread_id == state["trace_id"]).first()
    if existing is not None:
        return {"content_id": existing.id, "status": "awaiting_review"}
    report = enforce_content(state.get("draft") or {}, template.output_schema, template.run_config)
    item = ContentItem(
        validation_report=report.model_dump(),
        provenance=state.get("rag_provenance"),
        tenant_id=state["params"].get("tenant_id"),
        task_id=state["task_id"],
        template_id=state["params"]["template_id"],
        payload=state.get("draft") or {},
        qc_score=state.get("qc_score"),
        revise_count=state.get("revise_count", 0),
        status="awaiting_review",
        thread_id=state["trace_id"],
    )
    session.add(item)
    session.flush()  # 先拿到 item.id

    record = QualityRecord(
        tenant_id=state["params"].get("tenant_id"),
        item_id=item.id,
        score=state.get("qc_score", 0.0),
        dimension_scores=state.get("dimension_scores", {}),
        source="auto",
        template_version=getattr(template, "version", 1),
        config_snapshot=state.get("quality_config_snapshot"),
    )
    session.add(record)
    _finalize_quality_evaluation(session, state)
    session.commit()
    notify_human_review(session, item, state.get("qc_score", 0.0))
    return {"content_id": item.id, "status": "awaiting_review"}


def human_review_node(state: GenState, session: Session) -> dict:
    """人工卡点节点：interrupt 暂停图等待人工裁决，恢复后应用裁决（P1-1）。

    本节点在 interrupt 之前无副作用；恢复执行时 interrupt() 直接返回裁决值。
    """
    decision = interrupt(
        {
            "content_id": state.get("content_id"),
            "qc_score": state.get("qc_score"),
            "dimension_scores": state.get("dimension_scores"),
        }
    )
    approved = bool(decision.get("approved"))
    reason = decision.get("reason") or ""
    score = float(decision.get("score") or 0.0)

    item = session.query(ContentItem).filter(ContentItem.id == state["content_id"]).first()
    if approved:
        template = _load_template(session, item.template_id)
        try:
            item.validation_report = enforce_content(
                item.payload, template.output_schema, template.run_config
            ).model_dump()
        except ContentValidationError as exc:
            raise ContentStateConflictError(f"题目确定性校验未通过: {exc}") from exc
    provenance = item.provenance or {}
    if approved and provenance.get("require_review"):
        if not decision.get("reference_verified") or not provenance.get("citations"):
            raise ContentStateConflictError("人工裁决必须核验真实参考来源")
        item.provenance = {
            **provenance,
            "reference_review": {
                "verified": True,
                "reviewer": decision.get("reviewer_id"),
                **reference_snapshot(provenance["citations"]),
            },
        }
    existing = (
        session.query(QualityRecord)
        .filter(
            QualityRecord.item_id == state["content_id"],
            QualityRecord.source == "manual_review",
        )
        .first()
    )
    if existing is not None and item.status in {"passed", "rejected"}:
        return {
            "status": "review_passed" if item.status == "passed" else "review_rejected",
            "content_id": item.id,
        }
    record = QualityRecord(
        tenant_id=state["params"].get("tenant_id"),
        item_id=state["content_id"],
        score=score,
        dimension_scores={"manual": score},
        source="manual_review",
        reviewer=str(decision.get("reviewer_id") or decision.get("reviewer") or "unknown"),
        reason=reason if not approved else None,
        template_version=getattr(
            _load_template(session, state["params"]["template_id"]), "version", 1
        ),
        config_snapshot=state.get("quality_config_snapshot"),
    )
    session.add(record)
    item.status = "passed" if approved else "rejected"
    item.qc_score = score
    session.commit()
    if not approved:
        notify_content_rejected(session, item, reason)
    return {
        "status": "review_passed" if approved else "review_rejected",
        "content_id": state.get("content_id"),
    }


# ---------------------------------------------------------------------------
# 条件边路由
# ---------------------------------------------------------------------------
def after_validate(state: GenState) -> str:
    """Invalid checkpoint drafts use bounded revision, never an unbounded generate loop."""
    if state.get("status") == "validated":
        return "qc"
    return (
        "revise"
        if state.get("revise_count", 0) < state.get("max_revise", _DEFAULT_MAX_REVISE)
        else "reject"
    )


def after_qc(state: GenState) -> str:
    """质检达标入库；灰区（阈值下方 margin 内）转人工卡点；低分且未达改版上限则改版；达上限则拒绝。

    灰区优先于改版：接近阈值的内容不消耗改版预算，直接交人工裁决（模板级开关，默认关闭）。
    """
    threshold = state.get("quality_threshold", _DEFAULT_QUALITY_THRESHOLD)
    score = state.get("qc_score", 0)
    if score >= threshold:
        return "store"
    hr = state.get("human_review_config") or {}
    if hr.get("enabled") and score >= threshold - float(hr.get("gray_margin", 10)):
        return "submit_review"
    if state.get("revise_count", 0) < state.get("max_revise", _DEFAULT_MAX_REVISE):
        return "revise"
    return "reject"


def build_graph(session: Session):
    """构建并编译状态机图。每个节点闭包注入当前数据库会话。"""
    builder = StateGraph(GenState)

    builder.add_node("generate", lambda s: generate_node(s, session))
    builder.add_node("validate", lambda s: validate_node(s, session))
    builder.add_node("qc", lambda s: qc_node(s, session))
    builder.add_node("revise", lambda s: revise_node(s, session))
    builder.add_node("store", lambda s: store_node(s, session))
    builder.add_node("reject", lambda s: reject_node(s, session))
    builder.add_node("submit_review", lambda s: submit_review_node(s, session))
    builder.add_node("human_review", lambda s: human_review_node(s, session))

    builder.add_edge(START, "generate")
    builder.add_edge("generate", "validate")
    builder.add_conditional_edges("validate", after_validate)
    builder.add_conditional_edges("qc", after_qc)
    # 改版后重新进入生成节点（携带质检反馈），直至达标或达上限
    builder.add_edge("revise", "generate")
    builder.add_edge("store", END)
    builder.add_edge("reject", END)
    # 灰区人工卡点：先落库（写检查点），再 interrupt 等待人工裁决，裁决后终态
    builder.add_edge("submit_review", "human_review")
    builder.add_edge("human_review", END)

    return builder.compile(checkpointer=_get_checkpointer())


def run_generation(
    task_id: str,
    template_id: str,
    params: dict,
    session: Session,
    thread_id: str,
) -> dict:
    """同一 thread 的新运行/续跑串行化，避免重复投递并发重生成。"""
    with _thread_execution_lock(session, thread_id):
        return _run_generation_locked(task_id, template_id, params, session, thread_id)


def _run_generation_locked(
    task_id: str,
    template_id: str,
    params: dict,
    session: Session,
    thread_id: str,
) -> dict:
    """生成单条内容的入口函数。

    从任务参数中读取模板与阈值，构建状态机并执行。

    续跑语义（PostgresSaver）：以 thread_id=task:idx 为粒度持久化检查点——
    对已完成的 thread 重复 invoke 直接返回落库终态（不重复生成），
    对中断的 thread 从最后检查点续跑；任务级重投/恢复因此幂等。
    """
    # 读取任务的改版/阈值/人工卡点配置
    template = _load_template(session, template_id)
    run_config = template.run_config or {}
    max_revise = int(run_config.get("max_revise", _DEFAULT_MAX_REVISE))
    quality_threshold = float(run_config.get("quality_threshold", _DEFAULT_QUALITY_THRESHOLD))
    human_review_config = run_config.get("human_review") or {}

    graph = build_graph(session)
    graph_config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(graph_config)
    has_checkpoint = bool(snapshot.values)
    if has_checkpoint and not snapshot.next:
        return dict(snapshot.values)
    if has_checkpoint and any(task.interrupts for task in snapshot.tasks):
        return {**snapshot.values, "status": "awaiting_review", "__interrupt__": True}

    lifecycle_start = time.perf_counter_ns()
    record_lifecycle_event(
        trace_id=thread_id,
        stage="workflow",
        event="resume" if has_checkpoint else "started",
        model="langgraph",
        task_id=task_id,
        template_id=template_id,
        tenant_id=params.get("tenant_id"),
        metadata={"thread_id": thread_id},
    )
    initial_state: GenState = {
        "task_id": task_id,
        "params": {
            **params,
            **({"quantity": 1} if "quantity" in params else {}),
            "template_id": template_id,
        },
        "draft": None,
        "qc_score": 0.0,
        "dimension_scores": {},
        "revise_count": 0,
        "trace_id": thread_id,
        "status": "generated",
        "content_id": None,
        "max_revise": max_revise,
        "quality_threshold": quality_threshold,
        "human_review_config": human_review_config,
    }
    # 检查点按 CHECKPOINTER_BACKEND 持久化（PostgresSaver）/ 进程内（MemorySaver）
    try:
        result = graph.invoke(None if has_checkpoint else initial_state, config=graph_config)
    except Exception as exc:  # noqa: BLE001 - record lifecycle failure then preserve error
        record_lifecycle_event(
            trace_id=thread_id,
            stage="workflow",
            event="failed",
            model="langgraph",
            task_id=task_id,
            template_id=template_id,
            tenant_id=params.get("tenant_id"),
            status="failed",
            reason=str(exc),
            success=False,
            latency_ms=elapsed_ms(lifecycle_start),
            metadata={"thread_id": thread_id},
        )
        raise

    interrupted = bool(result.get("__interrupt__"))
    final_status = "awaiting_review" if interrupted else result.get("status") or "finished"
    record_lifecycle_event(
        trace_id=thread_id,
        stage="workflow",
        event="finished",
        model="langgraph",
        task_id=task_id,
        template_id=template_id,
        tenant_id=params.get("tenant_id"),
        status=final_status,
        success=final_status not in {"failed", "cancelled"},
        latency_ms=elapsed_ms(lifecycle_start),
        metadata={"thread_id": thread_id, "interrupted": interrupted},
    )
    return result


# 便捷入口：使用独立数据库会话执行单条生成
def run_generation_standalone(task_id: str, template_id: str, params: dict) -> dict:
    """独立会话执行单条生成，供 worker 之外的便捷调用。

    注意 thread_id 口径：本入口以裸 task_id 作为 thread（单条便捷场景）；
    批量任务的断点续跑检查点为 task:idx 粒度（见 worker/tasks.py:61），
    两者互不共用 thread，检查点不会互相覆盖。
    """
    session = SessionLocal()
    try:
        return run_generation(task_id, template_id, params, session, thread_id=task_id)
    finally:
        session.close()


def resume_human_review(thread_id: str, decision: dict, session: Session) -> dict:
    with _thread_execution_lock(session, thread_id):
        result = _resume_human_review_locked(thread_id, decision, session)
        from app.models import GenerationTaskItem
        from app.worker.tasks import _update_parent_task_progress

        item = (
            session.query(GenerationTaskItem)
            .filter(GenerationTaskItem.thread_id == thread_id)
            .first()
        )
        if item is not None and result.get("status") in {"review_passed", "review_rejected"}:
            item.status = item_status_from_graph(result["status"])
            from datetime import datetime, timezone

            item.finished_at = datetime.now(timezone.utc)
            session.commit()
            _update_parent_task_progress(session, item.task_id)
        return result


def _resume_human_review_locked(thread_id: str, decision: dict, session: Session) -> dict:
    """人工裁决后续跑图（P1-1）：从 human_review 节点的 interrupt 恢复。

    decision 形如 {"approved": bool, "score": float, "reason": str}。
    裁决落库（manual 质检记录 + 条目终态）由 human_review 节点完成。
    依赖持久化 checkpointer（PostgresSaver）：跨进程凭 thread_id 恢复。
    """
    graph = build_graph(session)
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    if not snapshot.values:
        raise CheckpointerUnavailableError(f"未找到待审核断点：{thread_id}")
    if not snapshot.next:
        return dict(snapshot.values)
    task_id = thread_id.split(":", 1)[0]
    started = time.perf_counter_ns()
    record_lifecycle_event(
        trace_id=thread_id,
        stage="workflow",
        event="resume",
        model="langgraph",
        task_id=task_id,
        metadata={"thread_id": thread_id, "resume_type": "human_review"},
    )
    try:
        result = graph.invoke(
            Command(resume=decision),
            config={"configurable": {"thread_id": thread_id}},
        )
    except Exception as exc:  # noqa: BLE001 - record resume failure then preserve error
        record_lifecycle_event(
            trace_id=thread_id,
            stage="workflow",
            event="failed",
            model="langgraph",
            task_id=task_id,
            status="failed",
            reason=str(exc),
            success=False,
            latency_ms=elapsed_ms(started),
            metadata={"thread_id": thread_id, "resume_type": "human_review"},
        )
        raise
    final_status = result.get("status") or "finished"
    record_lifecycle_event(
        trace_id=thread_id,
        stage="workflow",
        event="finished",
        model="langgraph",
        task_id=task_id,
        status=final_status,
        latency_ms=elapsed_ms(started),
        metadata={"thread_id": thread_id, "resume_type": "human_review"},
    )
    return result
