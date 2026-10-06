# app/schemas.py —— Pydantic v2 请求/响应模型
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMSchema(BaseModel):
    """允许从 SQLAlchemy ORM 对象直接构造/序列化的响应模型基类。"""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    """发起一次生成请求的入参。"""

    template_id: str
    params: Dict[str, Any]
    quantity: int = Field(default=1, ge=1, le=50)
    tenant_id: Optional[str] = None


class ModelProfileIn(BaseModel):
    """创建/更新模型档案的入参。"""

    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    provider_id: Optional[str] = None
    model_name: str = Field(min_length=1, max_length=128)
    cost_tier: str = "standard"
    is_default: bool = False
    status: str = Field(default="enabled", pattern="^(enabled|disabled)$")
    max_fallbacks: int = Field(default=1, ge=0, le=20)
    budget_per_task: Optional[float] = Field(default=None, ge=0)
    tenant_id: Optional[str] = None


class QualityReviewRequest(BaseModel):
    """人工质检标注入参。"""

    pass_: bool = Field(..., alias="pass")
    reason: str = ""
    reference_verified: bool = False


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class TaskBase(ORMSchema):
    id: str
    template_id: str
    quantity: int
    status: Literal[
        "pending",
        "dispatched",
        "running",
        "awaiting_review",
        "succeeded",
        "partially_succeeded",
        "failed",
        "cancelled",
    ]
    progress: float = Field(ge=0, le=1)
    trace_ref: Optional[str] = None
    tenant_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TaskOut(TaskBase):
    """任务详情响应。"""

    params: Dict[str, Any]


class TaskListOut(BaseModel):
    """任务列表（分页）响应。"""

    total: int
    items: List[TaskOut]


class ContentOut(ORMSchema):
    """内容条目响应。"""

    id: str
    task_id: str
    template_id: str
    tenant_id: Optional[str] = None
    payload: Dict[str, Any]
    provenance: Optional[Dict[str, Any]] = None
    validation_report: Optional[Dict[str, Any]] = None
    qc_score: Optional[float] = None
    status: str
    revise_count: int
    cost: Optional[float] = None
    # LangGraph 图线程 ID（awaiting_review 条目凭此恢复人工裁决）
    thread_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ContentListOut(BaseModel):
    """内容检索（分页）响应。"""

    total: int
    items: List[ContentOut]


class QualityOut(ORMSchema):
    """质检记录响应。"""

    id: str
    item_id: str
    score: float
    dimension_scores: Dict[str, Any]
    source: str
    reviewer: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------------------
# 质检校准（J1 质量闭环）
# ---------------------------------------------------------------------------
class CalibrateRequest(BaseModel):
    """触发质检权重校准的入参。"""

    template_id: str
    tenant_id: Optional[str] = None
    alpha: float = Field(default=1.0, ge=0.0, le=2.0)
    min_samples: int = Field(default=20, ge=1)
    min_fp: int = Field(default=5, ge=1)


class CalibrationOut(ORMSchema):
    """校准记录响应。"""

    id: str
    template_id: str
    tenant_id: Optional[str] = None
    weights: Dict[str, Any]
    threshold: float
    default_weights: Dict[str, Any]
    default_threshold: float
    sample_size: int
    false_pass_cnt: int
    lenient: Dict[str, Any]
    rejection_rate: float
    note: str
    # 只读参考：本次校准样本集上 judge 与人工的二值判定一致性（P0-4）
    agreement: Optional[Dict[str, Any]] = None
    created_at: datetime


class ModelProfileOut(ORMSchema):
    """模型档案响应。"""

    id: str
    name: str
    provider: str
    provider_id: Optional[str] = None
    provider_config_hash: Optional[str] = None
    model_name: str
    model_hash: Optional[str] = None
    cost_tier: str
    is_default: bool
    status: str
    health_status: str
    failure_count: int
    cooldown_until: Optional[datetime] = None
    max_fallbacks: int
    budget_per_task: Optional[float] = None
    tenant_id: Optional[str] = None
    created_at: datetime


class ConfigAuditOut(ORMSchema):
    """配置变更审计事件响应。"""

    id: str
    entity_type: str
    entity_id: str
    action: str
    actor_id: Optional[str] = None
    tenant_id: Optional[str] = None
    before_snapshot: Optional[Dict[str, Any]] = None
    after_snapshot: Optional[Dict[str, Any]] = None
    created_at: datetime


class QualityStatsBucketOut(BaseModel):
    """按模板版本、租户、来源和审核者拆分的质检统计。"""

    template_id: Optional[str] = None
    template_version: Optional[int] = None
    tenant_id: Optional[str] = None
    source: str
    reviewer: Optional[str] = None
    config_hash: str
    effective_threshold: float
    total: int
    passed: int
    rejected: int
    avg_score: float


class QualityStatsOut(BaseModel):
    """质量闭环统计结果及其实际查询口径。"""

    threshold: float
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    buckets: List[QualityStatsBucketOut]


class TemplateOut(ORMSchema):
    """题型模板响应。"""

    id: str
    type_id: str
    name: str
    version: int
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    quality_rules: List[Any]
    gen_prompt: Dict[str, Any]
    run_config: Dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime


class CostOut(BaseModel):
    """成本聚合结果（按维度归并：题型/模型/任务）。"""

    name: str
    total_cost: float
    count: int


# ---------------------------------------------------------------------------
# 深度成本报表（P3 / K2）
# ---------------------------------------------------------------------------
class CostStageOut(BaseModel):
    """按调用阶段（生成/质检）聚合的成本与 token。"""

    stage: str
    count: int
    total_cost: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cost: float
    completion_cost: float


class CostByKeyOut(BaseModel):
    """按某一维度（题型/模型/任务）聚合的成本与 token。"""

    name: str
    count: int
    total_cost: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class CostTraceOut(BaseModel):
    """单条调用成本明细（供下钻）。"""

    trace_id: str
    model: Optional[str] = None
    stage: str = ""
    template_id: Optional[str] = None
    task_id: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost: Optional[float] = None
    latency_ms: Optional[float] = None
    created_at: datetime


class CostDeepOut(BaseModel):
    """深度成本报表聚合结果。"""

    unknown_usage_calls: int = 0
    unknown_pricing_calls: int = 0
    total_cost: float
    total_count: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    # 按生成/质检阶段拆分
    stages: List[CostStageOut]
    # 多维聚合（题型/模型/任务）
    by_template: List[CostByKeyOut]
    by_model: List[CostByKeyOut]
    by_task: List[CostByKeyOut]
    # 单条调用下钻（最近 N 条）
    traces: List[CostTraceOut]


# ---------------------------------------------------------------------------
# 指标看板
# ---------------------------------------------------------------------------
class DashboardKpi(BaseModel):
    """看板核心指标项（含目标值与达标判定）。"""

    key: str
    label: str
    value: Optional[float]
    unit: str = ""
    target: Optional[float] = None
    # goal: higher_better / lower_better（仅达标判定方向）
    goal: str = "higher_better"


class DashboardOut(BaseModel):
    """指标看板聚合结果（PRD 第 15.1 节口径）。"""

    kpis: List[DashboardKpi]
    quality_pipeline: dict[str, int | float | None] = Field(default_factory=dict)
    # 按题型的质检通过率/人工驳回率明细
    by_template: List[dict]
    # 近 N 条任务的生产周期明细
    task_latency: List[dict]
    total_cost: float
    generated_count: int
    published_count: int


# ---------------------------------------------------------------------------
# Trace 链路回放
# ---------------------------------------------------------------------------
class TraceOut(ORMSchema):
    """单条 TraceLog 记录（链路中的一步 LLM 调用）。"""

    id: str
    snapshot_status: str = "legacy_unavailable"
    trace_id: str
    task_id: Optional[str] = None
    template_id: Optional[str] = None
    item_id: Optional[str] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    input_data: Optional[Dict[str, Any]] = None
    output_data: Optional[Dict[str, Any]] = None
    latency_ms: Optional[float] = None
    cost: Optional[float] = None
    # 调用阶段：generate 生成 / qc 质检
    stage: str = "generate"
    # token 细分（供深度成本分析）
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    # 本次调用为第几次尝试、是否通过校验（结构化符合率埋点）
    attempt: int = 1
    success: bool = True
    created_at: datetime


class TraceSummaryOut(BaseModel):
    """trace 链路摘要（列表项，按 trace_id 聚合）。"""

    trace_id: str
    task_id: Optional[str] = None
    template_id: Optional[str] = None
    call_count: int
    total_cost: float
    total_latency_ms: float
    first_at: datetime
    last_at: datetime


class TraceListOut(BaseModel):
    """trace 链路列表（分页）。"""

    total: int
    items: List[TraceSummaryOut]


class StructuredStatsOut(BaseModel):
    """结构化输出符合率统计（P0-2，口径见 docs/优化技术设计2.0.md §7）。"""

    total_generations: int
    success_generations: int
    failed_generations: int
    # 无样本或无成功样本时为 None（区别于 0）
    first_pass_rate: Optional[float] = None
    avg_attempts: Optional[float] = None
    failure_rate: Optional[float] = None


class GenerateResponse(BaseModel):
    """发起生成后的响应。"""

    task_id: str
    status: str = "pending"


class CancelTaskResponse(BaseModel):
    """取消任务响应。"""

    task_id: str
    status: str
    cancel_requested_at: datetime
    message: str = "取消请求已提交，运行中的 item 将在当前批次完成后停止"


class HealthOut(BaseModel):
    """健康检查响应。"""

    status: str = "ok"


class HealthDependencyOut(BaseModel):
    """健康探针响应；detail 只允许状态摘要，不包含密钥。"""

    name: str
    status: str
    latency_ms: float
    detail: str | None = None


class HealthReportOut(BaseModel):
    """依赖健康汇总响应。"""

    status: str
    ready: bool | None = None
    blocking: List[str] = Field(default_factory=list)
    dependencies: List[HealthDependencyOut]


# ---------------------------------------------------------------------------
# 站内消息通知
# ---------------------------------------------------------------------------
class NotificationOut(ORMSchema):
    """站内消息通知条目。"""

    id: str
    type: str
    title: str
    content: str
    related_id: Optional[str] = None
    is_read: bool
    created_at: datetime


class NotificationListOut(BaseModel):
    """站内消息列表（分页）+ 未读数。"""

    total: int
    unread: int
    items: List[NotificationOut]


class UnreadCountOut(BaseModel):
    """未读通知数。"""

    count: int


# ---------------------------------------------------------------------------
# RAG 知识库
# ---------------------------------------------------------------------------
class KnowledgeUploadIn(BaseModel):
    """上传一份知识资料并分块索引。"""

    chunk_layout: Literal["legacy", "structure"] | None = None
    source_type: str  # 教材 / 课标 / 真题
    source_name: str
    text: str
    knowledge_point: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    knowledge_points: List[str] = Field(default_factory=list, max_length=16)


class KnowledgeUploadOut(BaseModel):
    """索引结果。"""

    chunks: int
    source_type: str
    source_name: str
    knowledge_point: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunkOut(ORMSchema):
    """知识分块条目。"""

    id: str
    source_type: str
    source_name: str
    knowledge_point: Optional[str] = None
    content: str
    created_at: datetime
    parent_chunk_id: Optional[str] = None
    prev_chunk_id: Optional[str] = None
    next_chunk_id: Optional[str] = None
    chunk_type: Optional[str] = None
    knowledge_point_ids: Optional[List[str]] = None
    knowledge_point_labels: Optional[List[str]] = None
    document_id: Optional[str] = None
    chunk_index: Optional[int] = None
    content_start: Optional[int] = None
    content_end: Optional[int] = None
    page_no: Optional[int] = None
    context_header: Optional[str] = None
    section_path: Optional[List[str]] = None
    parser_version: Optional[str] = None
    chunker_version: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimension: Optional[int] = None
    meta: Optional[Dict[str, Any]] = None


class KnowledgeListOut(BaseModel):
    """知识分块列表（分页）。"""

    total: int
    items: List[KnowledgeChunkOut]


class KnowledgeRetrieveOut(BaseModel):
    """检索结果。"""

    query: str
    snippets: List[str]
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    protocol_version: str = "rag-context-v1"
    bundles: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 高质量回流样本（数据回流 / J2）
# ---------------------------------------------------------------------------
class SamplePoolIn(BaseModel):
    """沉淀一条回流样本的入参。"""

    content_id: str
    source: str = "manual"
    purpose: str = "sft"


class SamplePoolOut(ORMSchema):
    """回流样本条目。"""

    id: str
    item_id: str
    template_id: str
    source: str
    purpose: str
    knowledge_point: Optional[str] = None
    payload: Dict[str, Any]
    meta: Optional[Dict[str, Any]] = None
    created_at: datetime


class SamplePoolListOut(BaseModel):
    """回流样本列表（分页）。"""

    total: int
    items: List[SamplePoolOut]


class SampleSyncOut(BaseModel):
    """自动同步结果。"""

    added: int
    total: int


# ---------------------------------------------------------------------------
# 认证与用户管理（K4 权限）
# ---------------------------------------------------------------------------
# 合法角色取值
VALID_ROLES = ("admin", "researcher", "reviewer", "viewer")


class LoginIn(BaseModel):
    """登录入参。"""

    username: str
    password: str


class LoginOut(BaseModel):
    """登录成功响应：返回 token 与用户信息。"""

    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(ORMSchema):
    """用户信息响应（不含密码哈希）。"""

    id: str
    username: str
    display_name: str
    role: str
    status: str
    tenant_id: Optional[str] = None
    created_at: datetime


class UserCreateIn(BaseModel):
    """创建用户入参。"""

    username: str
    password: str = Field(min_length=6)
    display_name: str = ""
    tenant_id: Optional[str] = None
    role: str = Field(default="viewer", pattern="^(admin|researcher|reviewer|viewer)$")


class UserUpdateIn(BaseModel):
    """更新用户入参（可选字段，缺省不更新）。"""

    display_name: Optional[str] = None
    role: Optional[str] = Field(default=None, pattern="^(admin|researcher|reviewer|viewer)$")
    password: Optional[str] = Field(default=None, min_length=6)
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")


class UserListOut(BaseModel):
    """用户列表（分页）。"""

    total: int
    items: List[UserOut]


# 前向引用解析（LoginOut 引用了 UserOut）
LoginOut.model_rebuild()
