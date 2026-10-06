# app/models.py —— SQLAlchemy 2.0 声明式模型
# 实现平台的 6 张核心表，字段对照架构文档第 7 节。
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_uuid() -> str:
    """生成字符串形式的 UUID 主键。"""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# ---------------------------------------------------------------------------
# 1. 题型模板表
# ---------------------------------------------------------------------------
class QuestionTemplate(Base):
    """题型模板：定义生成某类题型所需的输入/输出结构、质量规则与提示词。"""

    __tablename__ = "question_template"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # 题型唯一标识，如 single_choice
    type_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    template_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    skill_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality_rules: Mapped[list] = mapped_column(JSONB, nullable=False)
    gen_prompt: Mapped[dict] = mapped_column(JSONB, nullable=False)
    run_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="enabled")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# 2. 生成任务表
# ---------------------------------------------------------------------------
class GenerationTask(Base):
    """一次批量生成请求，记录切片子任务与整体进度。"""

    __tablename__ = "generation_task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','dispatched','running','awaiting_review','succeeded',"
            "'partially_succeeded','failed','cancelled')",
            name="ck_generation_task_status",
        ),
        Index(
            "uq_active_generation_request",
            "request_hash",
            unique=True,
            postgresql_where=text(
                "request_hash IS NOT NULL AND status IN "
                "('pending','dispatched','running','awaiting_review')"
            ),
            sqlite_where=text(
                "request_hash IS NOT NULL AND status IN "
                "('pending','dispatched','running','awaiting_review')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("question_template.type_id"), nullable=False, index=True
    )
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 去重指纹：template_id + 规范化 params 的哈希，用于拦截重复提交
    request_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 状态流转：pending -> running -> partially_succeeded/succeeded/failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trace_ref: Mapped[str] = mapped_column(String(128), nullable=True)
    # 创建任务的用户；保留 nullable 兼容历史任务
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[dict] = mapped_column(JSONB, nullable=True)
    version_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=True)
    cancel_requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GenerationTaskItem(Base):
    """单个子任务条目：记录 thread_id、状态、失败原因、重试次数。"""

    __tablename__ = "generation_task_item"
    __table_args__ = (
        UniqueConstraint("task_id", "item_index", name="uq_generation_task_item_index"),
        CheckConstraint(
            "status IN ('pending','running','awaiting_review','succeeded','failed','cancelled')",
            name="ck_generation_item_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generation_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    content_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("content_item.id", ondelete="SET NULL"), nullable=True
    )
    failure_code: Mapped[str] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaskOutbox(Base):
    """任务投递 Outbox：任务事务提交后由 relay 投递到 Celery。"""

    __tablename__ = "task_outbox"
    __table_args__ = (UniqueConstraint("event_id", name="uq_task_outbox_event_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generation_task.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_base: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# 3. 内容条目表
# ---------------------------------------------------------------------------
class ContentItem(Base):
    """生成出的单条内容（含题干/选项/答案等载荷），待质检。"""

    __tablename__ = "content_item"
    __table_args__ = (UniqueConstraint("thread_id", name="uq_content_item_thread_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("generation_task.id"), index=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    validation_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    qc_score: Mapped[float] = mapped_column(Float, nullable=True)
    # 状态流转：pending_qc -> passed / rejected / published；
    # awaiting_review（P1-1 灰区人工卡点，interrupt 暂停等待人工裁决）
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_qc")
    revise_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Float, nullable=True)
    # LangGraph 图线程 ID（thread_id=task:idx）；awaiting_review 条目凭此恢复裁决
    thread_id: Mapped[str] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=True)
    published_by: Mapped[str] = mapped_column(String(36), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# 4. 质检记录表
# ---------------------------------------------------------------------------
class QualityRecord(Base):
    """自动/人工质检结果，含总分与各维度分。"""

    __tablename__ = "quality_record"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    item_id: Mapped[str] = mapped_column(ForeignKey("content_item.id"), index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    dimension_scores: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 来源：auto（自动） / manual_review（人工）
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    reviewer: Mapped[str] = mapped_column(String(64), nullable=True)
    # 人工驳回原因（仅 manual_review 且驳回时填写，供反向校准分析）
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    template_version: Mapped[int] = mapped_column(Integer, nullable=True)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 5. 模型档案表
# ---------------------------------------------------------------------------
class QualityEvaluation(Base):
    """每条草稿的自动质检事件，独立于内容是否达标/入库。"""

    __tablename__ = "quality_evaluation"
    __table_args__ = (UniqueConstraint("thread_id", "revise_count", name="uq_quality_eval_round"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("generation_task.id"), index=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    revise_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    dimension_scores: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelProvider(Base):
    """Admin-owned OpenAI-compatible endpoint; secrets never appear in output schemas."""

    __tablename__ = "model_provider"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_revision: Mapped[str] = mapped_column(String(36), nullable=False, default=new_uuid)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ModelRoute(Base):
    """Persistent UI overrides, separate from YAML which startup synchronizes."""

    __tablename__ = "model_route"
    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    judge_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ModelProfile(Base):
    """模型配置档案，用于路由主/备/默认模型。"""

    __tablename__ = "model_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("model_provider.id"), nullable=True)
    provider_config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_record: Mapped[ModelProvider | None] = relationship(ModelProvider)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    cost_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="enabled")
    health_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    max_fallbacks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    budget_per_task: Mapped[float] = mapped_column(Float, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index(
            "uq_default_model_scope",
            func.coalesce(tenant_id, ""),
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 6. 调用链路日志表
# ---------------------------------------------------------------------------
class TraceSnapshot(Base):
    """Bounded encrypted, redacted request/response; never sent to external trace sinks."""

    __tablename__ = "trace_snapshot"
    trace_row_id: Mapped[str] = mapped_column(
        ForeignKey("trace_log.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    replay_level: Mapped[str] = mapped_column(String(48), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class TraceLog(Base):
    """记录每次 LLM 调用的链路信息，用于成本与可观测性分析。"""

    __tablename__ = "trace_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    item_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    # 所属题型（成本按题型聚合，避免依赖 item_id 关联）
    template_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    # 所属生成任务（用于成本按任务聚合，与 item_id 解耦）
    task_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    snapshot_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="legacy_unavailable",
        server_default="legacy_unavailable",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=True)
    input_data: Mapped[dict] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    usage_reported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prompt_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost: Mapped[float] = mapped_column(Float, nullable=True)
    # 调用阶段：generate（生成）/ qc（质检），供深度成本按阶段拆分
    stage: Mapped[str] = mapped_column(String(32), nullable=True, index=True)
    # token 细分（供深度成本报表按生成/质检拆分 token 与成本）
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=True)
    # 本次调用为第几次尝试（1 起）；失败尝试同样落行（success=False），供首过率统计
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 本次调用是否通过 Pydantic 二次校验
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 7. 站内消息表（通知）
# ---------------------------------------------------------------------------
class AppNotification(Base):
    """站内消息通知：任务完成/失败/内容被驳回等事件的通知。"""

    __tablename__ = "app_notification"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # 通知类型：task_succeeded / task_partial / task_failed / content_rejected
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 关联对象（生成任务或缺值内容条目）
    related_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=True, unique=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 12. 配置变更审计表（P2-3）
# ---------------------------------------------------------------------------
class ConfigAuditEvent(Base):
    """模板/模型等可运行配置的不可变变更记录。"""

    __tablename__ = "config_audit_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    before_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# ---------------------------------------------------------------------------
# 9. 高质量回流样本表（数据回流 / J2）
# ---------------------------------------------------------------------------
class SamplePool(Base):
    """高质量回流样本：人工通过/发布的高质量内容沉淀为 few-shot / 微调语料。

    沉淀来源 source：manual（人工挑选）/ auto（自动同步人工通过项）。
    语料用途 purpose：fewshot / sft，供后续检索与导出消费。
    payload 为内容载荷快照，导出为 JSONL 语料时无需回查 content_item。
    """

    __tablename__ = "sample_pool"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # 关联内容条目（人工通过/已发布，判定为高质量）
    item_id: Mapped[str] = mapped_column(
        ForeignKey("content_item.id"), unique=True, nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 沉淀来源：manual / auto
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    # 语料用途：fewshot / sft
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="sft")
    # 知识点标签（取自生成任务参数，供检索）
    knowledge_point: Mapped[str] = mapped_column(String(128), nullable=True, index=True)
    # 内容载荷快照（独立快照，导出无需回查内容表）
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 10. 质检校准记录表（J1 质量闭环）
# ---------------------------------------------------------------------------
class QualityCalibration(Base):
    """质检权重校准记录：用人工驳回样本反向校准 judge 维度权重。

    每次校准生成一条记录（append-only），get_effective_weights 取最新一条
    作为当前生效的覆盖权重。模板 quality_rules 保持默认值不被修改。
    """

    __tablename__ = "quality_calibration"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 校准后的生效权重（覆盖模板默认权重）
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    # 模板默认权重快照（供对比展示）
    default_weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    default_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    # 校准元信息
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_pass_cnt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 每维放水度（lenient_i = mean(FP 维度分) - mean(通过集 维度分)）
    lenient: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rejection_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 8. 知识库分块表（RAG）
# ---------------------------------------------------------------------------
class KnowledgeDocument(Base):
    """Normalized source snapshot and parser diagnostics; old chunks need no document."""

    __tablename__ = "knowledge_document"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="indexed")
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeChunk(Base):
    """知识库分块：教材/课标/真题等资料按知识点分块并向量化，供生成时检索注入。"""

    __tablename__ = "knowledge_chunk"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_document.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_chunk_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_chunk.id", ondelete="CASCADE"), nullable=True, index=True
    )
    prev_chunk_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_chunk.id", ondelete="SET NULL"), nullable=True
    )
    next_chunk_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_chunk.id", ondelete="SET NULL"), nullable=True
    )
    chunk_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge_point_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    knowledge_point_labels: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_path: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunker_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 资料类型：教材 / 课标 / 真题
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 知识点标签，用于按知识点精确过滤
    knowledge_point: Mapped[str] = mapped_column(String(128), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 向量（维度与 embedding 模型输出一致，默认 1024）
    embedding: Mapped[list | None] = mapped_column(Vector(1024), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 10. 平台用户表（K4 权限 / 多角色）
# ---------------------------------------------------------------------------
class User(Base):
    """平台用户：登录认证 + 角色权限。

    role 取值：admin（管理员）/ researcher（教研员）/ reviewer（质检员）/ viewer（查看者）。
    单租户多角色模式下 tenant_id 暂留空，为后续对外化多租户预留。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # bcrypt 哈希后的密码，绝不落明文
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
    # 状态：active（正常）/ disabled（禁用，禁用后不可登录与访问）
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OcrJob(Base):
    """Durable parsing job, independent from knowledge indexing or content generation."""

    __tablename__ = "ocr_job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_key: Mapped[str] = mapped_column(String(256), nullable=False)
    input_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_pages: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    dispatch_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_requested")
    approval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    index_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    index_dispatch_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    index_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    indexed_document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_document.id", ondelete="SET NULL"), nullable=True
    )
    preview_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    preview_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OcrPage(Base):
    """Per-page checkpoint; committed successful pages survive cancellation/restarts."""

    __tablename__ = "ocr_page"
    __table_args__ = (UniqueConstraint("job_id", "page_no", name="uq_ocr_page_job_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ocr_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    result_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OcrBoundaryJob(Base):
    """Separate explicit local multi-page review; never rewrites successful OCR/index data."""

    __tablename__ = "ocr_boundary_job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("ocr_job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pages: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    source_preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
