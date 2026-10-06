# app/api/routes.py —— FastAPI 业务接口
import json
from datetime import datetime
from typing import Any, List, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_config_change
from app.config import settings
from app.database import get_db
from app.engine.metrics import compute_structured_stats
from app.engine.quality_metrics import quality_pipeline_stats
from app.health import dependency_report, readiness_report
from app.models import (
    ConfigAuditEvent,
    ContentItem,
    GenerationTask,
    ModelProfile,
    QualityEvaluation,
    QualityRecord,
    QuestionTemplate,
    TraceLog,
    User,
)
from app.sample_pool import (
    list_samples,
)
from app.schemas import (
    CalibrateRequest,
    CalibrationOut,
    CancelTaskResponse,
    ConfigAuditOut,
    ContentListOut,
    ContentOut,
    CostByKeyOut,
    CostDeepOut,
    CostOut,
    CostStageOut,
    CostTraceOut,
    DashboardKpi,
    DashboardOut,
    GenerateRequest,
    GenerateResponse,
    HealthOut,
    HealthReportOut,
    KnowledgeChunkOut,
    KnowledgeListOut,
    KnowledgeRetrieveOut,
    KnowledgeUploadIn,
    KnowledgeUploadOut,
    LoginIn,
    LoginOut,
    ModelProfileIn,
    ModelProfileOut,
    NotificationListOut,
    NotificationOut,
    QualityOut,
    QualityReviewRequest,
    QualityStatsBucketOut,
    QualityStatsOut,
    SamplePoolIn,
    SamplePoolListOut,
    SamplePoolOut,
    SampleSyncOut,
    StructuredStatsOut,
    TaskListOut,
    TaskOut,
    TemplateOut,
    TraceListOut,
    TraceOut,
    TraceSummaryOut,
    UnreadCountOut,
    UserCreateIn,
    UserListOut,
    UserOut,
    UserUpdateIn,
)
from app.security import (
    get_current_user,
    require_permission,
)

# Service 层导入（P1-1 分层重构）
from app.services import (
    AuthService,
    ContentService,
    GenerationService,
    KnowledgeService,
    NotificationService,
    QualityService,
    SampleService,
)
from app.services.knowledge_service import StructureReview
from app.tenancy import require_scope, scope_query
from app.versioning import ensure_model_profile_hash, hash_value
from app.worker.celery_app import celery_app

router = APIRouter()


# 正在执行、不应重复提交的任务状态
_ACTIVE_TASK_STATUSES = ("pending", "running")


# ---------------------------------------------------------------------------
# 认证（登录 / 当前用户）
# ---------------------------------------------------------------------------
@router.post("/api/auth/login", response_model=LoginOut)
def login(req: LoginIn, db: Session = Depends(get_db)):
    """用户登录：校验用户名密码，成功签发 JWT。"""
    service = AuthService(db)
    return service.login(req.username, req.password)


@router.get("/api/auth/me", response_model=UserOut)
def get_me(user: User = Depends(get_current_user)):
    """返回当前登录用户信息。"""
    return user


# ---------------------------------------------------------------------------
# 用户管理（仅管理员）
# ---------------------------------------------------------------------------
@router.get("/api/users", response_model=UserListOut)
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user:manage")),
):
    """用户列表（分页），仅管理员可访问。"""
    service = AuthService(db)
    users, total = service.list_users(skip=(page - 1) * page_size, limit=page_size)
    return UserListOut(items=users, total=total)


@router.post("/api/users", response_model=UserOut)
def create_user(
    req: UserCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user:manage")),
):
    """创建用户（管理员）。用户名唯一，密码 bcrypt 哈希入库。"""
    service = AuthService(db)
    return service.create_user(req, current_user.id, tenant_id=req.tenant_id)


@router.patch("/api/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    req: UserUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("user:manage")),
):
    """更新用户（管理员）：改显示名/角色/密码/状态。"""
    service = AuthService(db)
    return service.update_user(user_id, req)


# ---------------------------------------------------------------------------
# 发起生成
# ---------------------------------------------------------------------------
@router.post("/api/generate", response_model=GenerateResponse)
def create_generate_task(
    req: GenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("generate:create")),
):
    """创建生成任务并投递到 Celery 队列。

    相同题型+参数且仍在执行中的任务会被去重拦截（返回 409）。
    """
    service = GenerationService(db)
    return service.create_task(req, current_user, celery_app)


# ---------------------------------------------------------------------------
# 任务查询
# ---------------------------------------------------------------------------
@router.get("/api/tasks/{task_id}", response_model=TaskOut)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("task:read")),
):
    """查询单个任务详情与进度。"""
    service = GenerationService(db)
    return service.get_task(task_id, current_user)


@router.post("/api/tasks/{task_id}/cancel", response_model=CancelTaskResponse)
def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("generate:cancel")),
):
    """取消待执行或运行中的任务。

    合作式退出：设置 cancel_requested_at 标记，worker 在下一个 item 前检查并退出。
    已完成的任务不可取消。
    """
    from datetime import datetime, timezone

    task = db.query(GenerationTask).filter(GenerationTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    require_scope(task, current_user)
    # Waiting human review is protected from delivery cancellation.
    from app.domain.status import DELIVERY_TASK_STATUSES

    if task.status not in DELIVERY_TASK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {task.status}，不可取消（仅 pending/dispatched/running 可取消）",
        )

    # 设置取消标记
    task.cancel_requested_at = datetime.now(timezone.utc)
    db.commit()

    return CancelTaskResponse(
        task_id=task.id,
        status=task.status,
        cancel_requested_at=task.cancel_requested_at,
    )


@router.get("/api/tasks", response_model=TaskListOut)
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("task:read")),
):
    """任务列表（分页）。"""
    service = GenerationService(db)
    return service.list_tasks(page, page_size, current_user=current_user)


# ---------------------------------------------------------------------------
# 内容检索与操作
# ---------------------------------------------------------------------------
@router.get("/api/contents", response_model=ContentListOut)
def list_contents(
    template_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("content:read")),
):
    """内容检索（可按 template_id / status 筛选，P0-3 租户隔离）。"""
    service = ContentService(db)
    return service.list_contents(
        current_user=current_user,
        template_id=template_id,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/api/contents/{content_id}", response_model=ContentOut)
def get_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("content:read")),
):
    """内容详情（P0-3 租户隔离）。"""
    service = ContentService(db)
    return service.content_out(service.get_content(content_id, current_user))


@router.post("/api/contents/{content_id}/publish", response_model=ContentOut)
def publish_content(
    content_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("content:publish")),
):
    """发布内容（P0-2 状态校验）：仅 passed 可发布，记录操作人与时间。"""
    service = ContentService(db)
    return service.publish_content(content_id, current_user)


# ---------------------------------------------------------------------------
# 人工质检
# ---------------------------------------------------------------------------
@router.post("/api/quality/{content_id}/review", response_model=QualityOut)
def review_content(
    content_id: str,
    req: QualityReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("quality:review")),
):
    """人工质检标注：通过/驳回，写入 QualityRecord。

    灰区人工卡点（P1-1）：条目若由 LangGraph interrupt 暂停（thread_id 存在且
    status=awaiting_review），凭 Command(resume) 恢复图，由 human_review 节点
    完成裁决落库；存量条目走旧路径。
    """
    service = QualityService(db)
    return service.review_content(content_id, req, current_user)


# ---------------------------------------------------------------------------
# 质检校准（J1 质量闭环）
# ---------------------------------------------------------------------------
@router.post("/api/quality/calibrate", response_model=CalibrationOut)
def calibrate_quality(
    req: CalibrateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("quality:review")),
):
    """触发质检权重校准：用人工驳回样本反向校准 judge 维度权重。

    闭环：低分自动改版 → 人工驳回记录 → 假阳性样本 → 计算放水度 → 降权。
    样本不足时写入"样本不足"记录但不改变默认权重。
    """
    service = QualityService(db)
    return service.calibrate_quality(req, current_user)


@router.get("/api/quality/calibration", response_model=list[CalibrationOut])
def get_calibrations(
    template_id: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """查询校准记录列表（可按模板过滤），按时间倒序。"""
    service = QualityService(db)
    return service.list_calibrations(template_id, current_user=current_user)


@router.get("/api/quality/stats", response_model=QualityStatsOut)
def quality_stats(
    template_id: str | None = Query(None),
    template_version: int | None = Query(None, ge=1),
    tenant_id: str | None = Query(None),
    source: str | None = Query(None, pattern="^(auto|manual_review)$"),
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """按模板版本/租户/时间窗口统计质量闭环，避免混用不同配置快照。"""
    scoped_tenant = current_user.tenant_id if current_user.role != "admin" else tenant_id
    query = scope_query(
        db.query(QualityRecord, ContentItem.template_id), QualityRecord, current_user
    ).join(ContentItem, ContentItem.id == QualityRecord.item_id)
    if template_id:
        query = query.filter(ContentItem.template_id == template_id)
    if template_version is not None:
        query = query.filter(QualityRecord.template_version == template_version)
    if scoped_tenant is not None:
        query = query.filter(QualityRecord.tenant_id == scoped_tenant)
    if source:
        query = query.filter(QualityRecord.source == source)
    if start_at:
        query = query.filter(QualityRecord.created_at >= start_at)
    if end_at:
        query = query.filter(QualityRecord.created_at < end_at)
    rows = query.order_by(QualityRecord.created_at.asc()).all()
    buckets: dict[tuple, dict] = {}
    for record, item_template_id in rows:
        snapshot = record.config_snapshot or {}
        bucket_key = (
            item_template_id,
            record.template_version,
            record.tenant_id,
            record.source,
            record.reviewer,
            hash_value(snapshot),
        )
        bucket = buckets.setdefault(
            bucket_key,
            {
                "template_id": item_template_id,
                "template_version": record.template_version,
                "tenant_id": record.tenant_id,
                "source": record.source,
                "reviewer": record.reviewer,
                "config_hash": hash_value(snapshot),
                "effective_threshold": float(
                    snapshot["threshold"]
                    if snapshot.get("threshold") is not None
                    else settings.QUALITY_THRESHOLD
                ),
                "total": 0,
                "passed": 0,
                "rejected": 0,
                "scores": [],
            },
        )
        threshold = bucket["effective_threshold"]
        bucket["total"] += 1
        bucket["passed"] += int(float(record.score) >= threshold)
        bucket["rejected"] += int(float(record.score) < threshold)
        bucket["scores"].append(float(record.score))
    return QualityStatsOut(
        threshold=settings.QUALITY_THRESHOLD,
        start_at=start_at,
        end_at=end_at,
        buckets=[
            QualityStatsBucketOut(
                template_id=value["template_id"],
                template_version=value["template_version"],
                tenant_id=value["tenant_id"],
                source=value["source"],
                reviewer=value["reviewer"],
                config_hash=value["config_hash"],
                effective_threshold=value["effective_threshold"],
                total=value["total"],
                passed=value["passed"],
                rejected=value["rejected"],
                avg_score=sum(value["scores"]) / len(value["scores"]),
            )
            for value in buckets.values()
        ],
    )


# ---------------------------------------------------------------------------
# 成本聚合
# ---------------------------------------------------------------------------
@router.get("/api/costs", response_model=list[CostOut])
def cost_aggregation(
    group_by: str = Query("template", pattern="^(template|model|task)$"),
    template_id: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """成本聚合：按 题型/模型/任务 维度归并每条 LLM 调用的成本。

    - group_by=template：按题型（TraceLog.template_id）聚合；
    - group_by=model：按模型聚合；
    - group_by=task：按生成任务（TraceLog.task_id）聚合。
    可选 template_id 过滤。未关联到题型/任务的调用归入 unknown 维度。
    """
    # 统一先关联内容条目（仅用于 template_id 过滤）
    base = scope_query(
        db.query(TraceLog, ContentItem.template_id.label("tpl_id")), TraceLog, current_user
    ).outerjoin(ContentItem, ContentItem.id == TraceLog.item_id)
    if template_id:
        base = base.filter(
            (ContentItem.template_id == template_id) | (TraceLog.template_id == template_id)
        )

    if group_by == "task":
        rows = (
            base.with_entities(
                func.coalesce(TraceLog.task_id, "unknown").label("name"),
                func.coalesce(func.sum(TraceLog.cost), 0.0).label("total_cost"),
                func.count(TraceLog.id).label("count"),
            )
            .group_by(TraceLog.task_id)
            .all()
        )
    elif group_by == "model":
        rows = (
            base.with_entities(
                func.coalesce(TraceLog.model, "unknown").label("name"),
                func.coalesce(func.sum(TraceLog.cost), 0.0).label("total_cost"),
                func.count(TraceLog.id).label("count"),
            )
            .group_by(TraceLog.model)
            .all()
        )
    else:  # template
        rows = (
            base.with_entities(
                func.coalesce(TraceLog.template_id, "unknown").label("name"),
                func.coalesce(func.sum(TraceLog.cost), 0.0).label("total_cost"),
                func.count(TraceLog.id).label("count"),
            )
            .group_by(TraceLog.template_id)
            .all()
        )

    return [CostOut(name=r.name, total_cost=float(r.total_cost), count=int(r.count)) for r in rows]


# ---------------------------------------------------------------------------
# 深度成本报表（P3 / K2）
# ---------------------------------------------------------------------------
def _cost_by_key(
    db: Session,
    key_col,
    unknown: str,
    current_user: User | None = None,
    template_id: str | None = None,
    task_id: str | None = None,
) -> list:
    """按指定列聚合成本与 token（coalesce 归入 unknown 维度）。"""
    rows = (
        scope_query(
            db.query(
                func.coalesce(key_col, unknown).label("name"),
                func.count(TraceLog.id).label("count"),
                func.coalesce(func.sum(TraceLog.cost), 0.0).label("total_cost"),
                func.coalesce(func.sum(TraceLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(TraceLog.completion_tokens), 0).label("completion_tokens"),
            ),
            TraceLog,
            current_user,
        )
        .filter(*([TraceLog.template_id == template_id] if template_id is not None else []))
        .filter(*([TraceLog.task_id == task_id] if task_id is not None else []))
        .group_by(key_col)
        .order_by(func.sum(TraceLog.cost).desc())
        .all()
    )
    return [
        CostByKeyOut(
            name=r.name,
            count=int(r.count),
            total_cost=float(r.total_cost),
            prompt_tokens=int(r.prompt_tokens),
            completion_tokens=int(r.completion_tokens),
            total_tokens=int(r.prompt_tokens) + int(r.completion_tokens),
        )
        for r in rows
    ]


@router.get("/api/costs/deep", response_model=CostDeepOut)
def cost_deep_report(
    template_id: str | None = Query(None),
    task_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """深度成本报表：按 生成/质检 阶段拆分 + 多维（题型/模型/任务）聚合 + 单条下钻。

    口径：
    - 阶段拆分：按 TraceLog.stage（generate/qc）聚合成本与 token，量化生成与质检各自开销；
    - 多维聚合：按 题型/模型/任务 归并成本与 token（unknown 兜底）；
    - 单条下钻：列出最近 N 条 LLM 调用明细（token/成本/耗时），可下钻到单条。
    """
    base = scope_query(db.query(TraceLog), TraceLog, current_user)
    if template_id:
        base = base.filter(TraceLog.template_id == template_id)
    if task_id:
        base = base.filter(TraceLog.task_id == task_id)

    # 全量合计
    agg = base.with_entities(
        func.count(TraceLog.id),
        func.coalesce(func.sum(TraceLog.cost), 0.0),
        func.coalesce(func.sum(TraceLog.prompt_tokens), 0),
        func.coalesce(func.sum(TraceLog.completion_tokens), 0),
    ).first()
    total_count, total_cost, total_prompt, total_completion = (
        int(agg[0]),
        float(agg[1]),
        int(agg[2]),
        int(agg[3]),
    )

    # 阶段拆分（按 stage 分组）
    stage_rows = (
        base.with_entities(
            TraceLog.stage,
            func.count(TraceLog.id),
            func.coalesce(func.sum(TraceLog.cost), 0.0),
            func.coalesce(func.sum(TraceLog.prompt_tokens), 0),
            func.coalesce(func.sum(TraceLog.completion_tokens), 0),
            func.coalesce(func.sum(TraceLog.prompt_cost), 0.0),
            func.coalesce(func.sum(TraceLog.completion_cost), 0.0),
        )
        .group_by(TraceLog.stage)
        .all()
    )
    stages = [
        CostStageOut(
            stage=r[0] or "unknown",
            count=int(r[1]),
            total_cost=float(r[2]),
            prompt_tokens=int(r[3]),
            completion_tokens=int(r[4]),
            total_tokens=int(r[3]) + int(r[4]),
            prompt_cost=round(float(r[5] or 0.0), 6),
            completion_cost=round(float(r[6] or 0.0), 6),
        )
        for r in stage_rows
    ]

    by_template = _cost_by_key(
        db, TraceLog.template_id, "unknown", current_user, template_id, task_id
    )
    by_model = _cost_by_key(db, TraceLog.model, "unknown", current_user, template_id, task_id)
    by_task = _cost_by_key(db, TraceLog.task_id, "unknown", current_user, template_id, task_id)

    # 单条下钻（最近 N 条）
    trace_rows = base.order_by(TraceLog.created_at.desc()).limit(limit).all()
    traces = [
        CostTraceOut(
            trace_id=t.trace_id,
            model=t.model,
            stage=t.stage or "",
            template_id=t.template_id,
            task_id=t.task_id,
            prompt_tokens=t.prompt_tokens,
            completion_tokens=t.completion_tokens,
            total_tokens=(t.prompt_tokens or 0) + (t.completion_tokens or 0),
            cost=t.cost,
            latency_ms=t.latency_ms,
            created_at=t.created_at,
        )
        for t in trace_rows
    ]

    return CostDeepOut(
        total_cost=total_cost,
        unknown_usage_calls=base.filter(
            TraceLog.usage_reported.is_not(True),
            TraceLog.stage.in_(["generate", "qc", "embedding"]),
        ).count(),
        unknown_pricing_calls=base.filter(
            TraceLog.prompt_cost.is_(None), TraceLog.stage.in_(["generate", "qc", "embedding"])
        ).count(),
        total_count=total_count,
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        total_tokens=total_prompt + total_completion,
        stages=stages,
        by_template=by_template,
        by_model=by_model,
        by_task=by_task,
        traces=traces,
    )


# ---------------------------------------------------------------------------
# 指标看板（PRD 第 15.1 节口径）
# ---------------------------------------------------------------------------
@router.get("/api/dashboard", response_model=DashboardOut)
def dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """指标看板聚合：生产量 / 质检通过率 / 人工驳回率 / 生产周期 / 成本。

    口径（对齐 PRD 15.1）：
    - 内容产出量：content_item 总数
    - 首过/最终通过率：独立评分事件，按各事件有效阈值，排除未完成最终轮
    - 人工驳回率：人工质检（source=manual_review）score=0 占比
    - 生产周期：generation_task created_at→updated_at 平均/最近时长
    - 单条成本：内容条目平均生成成本
    - 发布量：status=published 计数
    """
    # 内容总量与状态分布
    total_items = (
        scope_query(db.query(func.count(ContentItem.id)), ContentItem, current_user).scalar() or 0
    )
    published = (
        scope_query(db.query(func.count(ContentItem.id)), ContentItem, current_user)
        .filter(ContentItem.status == "published")
        .scalar()
        or 0
    )
    total_cost = (
        scope_query(
            db.query(func.coalesce(func.sum(TraceLog.cost), 0.0)), TraceLog, current_user
        ).scalar()
        or 0.0
    )
    avg_cost = float(total_cost) / total_items if total_items else 0.0

    # 新口径仅使用改造后的事件表，历史入库数据不能反推首过/淘汰分母。
    events = scope_query(db.query(QualityEvaluation), QualityEvaluation, current_user).all()
    pipeline_stats = quality_pipeline_stats(events)
    qc_pass_rate = pipeline_stats["final_pass_rate"]

    # 人工驳回率（source=manual_review，score=0 即驳回）
    manual_total = (
        scope_query(db.query(func.count(QualityRecord.id)), QualityRecord, current_user)
        .filter(QualityRecord.source == "manual_review")
        .scalar()
        or 0
    )
    manual_rejected = (
        scope_query(db.query(func.count(QualityRecord.id)), QualityRecord, current_user)
        .filter(QualityRecord.source == "manual_review")
        .filter(QualityRecord.score == 0)
        .scalar()
        or 0
    )
    manual_reject_rate = manual_rejected / manual_total if manual_total else 0.0

    # 生产周期：任务平均时长（秒）
    avg_latency_row = scope_query(
        db.query(
            func.avg(func.extract("epoch", GenerationTask.updated_at - GenerationTask.created_at))
        ),
        GenerationTask,
        current_user,
    ).scalar()
    avg_latency = float(avg_latency_row) if avg_latency_row is not None else 0.0

    # 按题型质检通过率明细
    by_template = _dashboard_by_template(db, current_user)

    # 近 N 条已完成任务的生产周期明细
    task_latency = _dashboard_task_latency(db, limit=10)

    # 结构化输出符合率指标（P0-2：generate 阶段逐调用行 -> thread 级统计）
    gen_rows = (
        scope_query(
            db.query(TraceLog.trace_id, TraceLog.attempt, TraceLog.success), TraceLog, current_user
        )
        .filter(TraceLog.stage == "generate")
        .all()
    )
    structured_stats = compute_structured_stats(
        [
            {
                "trace_id": r.trace_id,
                "attempt": r.attempt or 1,
                "success": bool(r.success),
            }
            for r in gen_rows
        ]
    )

    # 改版触发率：入库条目中经历过自动改版的占比
    revised_items = (
        scope_query(db.query(func.count(ContentItem.id)), ContentItem, current_user)
        .filter(ContentItem.revise_count >= 1)
        .scalar()
        or 0
    )
    revise_trigger_rate = revised_items / total_items if total_items else 0.0

    kpis = [
        DashboardKpi(
            key="generated",
            label="内容产出量",
            value=float(total_items),
            unit="条",
            target=None,
            goal="higher_better",
        ),
        DashboardKpi(
            key="published",
            label="已发布量",
            value=float(published),
            unit="条",
            target=None,
            goal="higher_better",
        ),
        DashboardKpi(
            key="qc_first_pass_rate",
            label="自动质检首过率",
            value=(
                round(pipeline_stats["first_pass_rate"] * 100, 2)
                if pipeline_stats["first_pass_rate"] is not None
                else None
            ),
            unit="%",
            goal="higher_better",
        ),
        DashboardKpi(
            key="judge_failure_rate",
            label="Judge 最终失败率",
            value=(
                round(pipeline_stats["quality_failure_rate"] * 100, 2)
                if pipeline_stats["quality_failure_rate"] is not None
                else None
            ),
            unit="%",
            goal="lower_better",
        ),
        DashboardKpi(
            key="qc_pass_rate",
            label="自动质检最终通过率",
            value=round(qc_pass_rate * 100, 2) if qc_pass_rate is not None else None,
            unit="%",
            target=90.0,
            goal="higher_better",
        ),
        DashboardKpi(
            key="manual_reject_rate",
            label="人工驳回率",
            value=round(manual_reject_rate * 100, 2),
            unit="%",
            target=5.0,
            goal="lower_better",
        ),
        DashboardKpi(
            key="avg_latency",
            label="平均生产周期",
            value=round(avg_latency, 1),
            unit="s",
            target=3600.0,
            goal="lower_better",
        ),
        DashboardKpi(
            key="avg_cost",
            label="单条平均成本",
            value=round(avg_cost, 6),
            unit="¥",
            target=None,
            goal="lower_better",
        ),
        DashboardKpi(
            key="structured_first_pass_rate",
            label="结构化首过率",
            value=round((structured_stats["first_pass_rate"] or 0.0) * 100, 2),
            unit="%",
            target=None,
            goal="higher_better",
        ),
        DashboardKpi(
            key="structured_avg_attempts",
            label="平均尝试次数",
            value=(
                round(structured_stats["avg_attempts"], 2)
                if structured_stats["avg_attempts"] is not None
                else 0.0
            ),
            unit="次",
            target=1.0,
            goal="lower_better",
        ),
        DashboardKpi(
            key="structured_failure_rate",
            label="结构化失败率",
            value=round((structured_stats["failure_rate"] or 0.0) * 100, 2),
            unit="%",
            target=0.0,
            goal="lower_better",
        ),
        DashboardKpi(
            key="revise_trigger_rate",
            label="改版触发率",
            value=round(revise_trigger_rate * 100, 2),
            unit="%",
            target=None,
            goal="lower_better",
        ),
    ]

    return DashboardOut(
        kpis=kpis,
        quality_pipeline=pipeline_stats,
        by_template=by_template,
        task_latency=task_latency,
        total_cost=float(total_cost or 0.0),
        generated_count=total_items,
        published_count=published,
    )


def _dashboard_by_template(db: Session, current_user: User | None = None) -> List[dict]:
    """按题型统计唯一评分 thread，避免审核记录 join 放大分母。"""
    events = scope_query(db.query(QualityEvaluation), QualityEvaluation, current_user).all()
    templates = sorted({event.template_id for event in events})
    result = []
    for template_id in templates:
        stats = quality_pipeline_stats(
            event for event in events if event.template_id == template_id
        )
        result.append(
            {
                "template_id": template_id,
                "generated": stats["threads"],
                "pass_rate": (
                    round(stats["final_pass_rate"] * 100, 2)
                    if stats["final_pass_rate"] is not None
                    else None
                ),
            }
        )
    return result


def _dashboard_task_latency(
    db: Session, limit: int = 10, current_user: User | None = None
) -> List[dict]:
    """近 N 条任务的生产周期明细（秒）。"""
    rows = (
        scope_query(
            db.query(
                GenerationTask.id,
                GenerationTask.template_id,
                GenerationTask.status,
                GenerationTask.created_at,
                GenerationTask.updated_at,
            ),
            GenerationTask,
            current_user,
        )
        .order_by(GenerationTask.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "task_id": r.id,
            "template_id": r.template_id,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "latency_s": (
                round((r.updated_at - r.created_at).total_seconds(), 1)
                if r.updated_at and r.created_at
                else None
            ),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Trace 链路回放
# ---------------------------------------------------------------------------
@router.get("/api/traces", response_model=TraceListOut)
def list_traces(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """列出最近的 trace 链路摘要（按 trace_id 去重聚合，最近优先）。

    每条含调用次数、累计成本、累计耗时与时间范围，便于在列表中选择链路回放。
    """
    total = (
        scope_query(
            db.query(func.count(func.distinct(TraceLog.trace_id))), TraceLog, current_user
        ).scalar()
        or 0
    )
    rows = (
        scope_query(
            db.query(
                TraceLog.trace_id,
                func.max(TraceLog.task_id).label("task_id"),
                func.max(TraceLog.template_id).label("template_id"),
                func.count(TraceLog.id).label("call_count"),
                func.coalesce(func.sum(TraceLog.cost), 0.0).label("total_cost"),
                func.coalesce(func.sum(TraceLog.latency_ms), 0.0).label("total_latency_ms"),
                func.min(TraceLog.created_at).label("first_at"),
                func.max(TraceLog.created_at).label("last_at"),
            ),
            TraceLog,
            current_user,
        )
        .group_by(TraceLog.trace_id)
        .order_by(func.max(TraceLog.created_at).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TraceListOut(
        total=total,
        items=[
            TraceSummaryOut(
                trace_id=r.trace_id,
                task_id=r.task_id,
                template_id=r.template_id,
                call_count=int(r.call_count),
                total_cost=float(r.total_cost),
                total_latency_ms=float(r.total_latency_ms),
                first_at=r.first_at,
                last_at=r.last_at,
            )
            for r in rows
        ],
    )


@router.get("/api/traces/structured-stats", response_model=StructuredStatsOut)
def structured_stats(
    template_id: str | None = Query(None, description="按题型过滤"),
    task_id: str | None = Query(None, description="按任务过滤"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """结构化输出符合率统计（P0-2）。

    generate 阶段每次调用一行（含校验失败尝试，attempt/success 落库），
    按 thread（trace_id）聚合出首过率 / 平均尝试次数 / 最终失败率。
    """
    q = scope_query(
        db.query(TraceLog.trace_id, TraceLog.attempt, TraceLog.success), TraceLog, current_user
    ).filter(TraceLog.stage == "generate")
    if template_id:
        q = q.filter(TraceLog.template_id == template_id)
    if task_id:
        q = q.filter(TraceLog.task_id == task_id)
    rows = q.all()
    stats = compute_structured_stats(
        [
            {
                "trace_id": r.trace_id,
                "attempt": r.attempt or 1,
                "success": bool(r.success),
            }
            for r in rows
        ]
    )
    return StructuredStatsOut(**stats)


@router.get("/api/traces/records/{row_id}/snapshot")
def get_trace_snapshot(
    row_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    from app.engine.trace_snapshots import read_snapshot

    row = require_scope(db.get(TraceLog, row_id), current_user)
    snapshot, data = read_snapshot(db, row.id)
    record_config_change(
        db,
        entity_type="trace_snapshot_access",
        entity_id=row.id,
        action="read",
        actor_id=current_user.id,
        tenant_id=row.tenant_id,
        before=None,
        after={"snapshot_hash": snapshot.content_hash, "replay_level": snapshot.replay_level},
    )
    db.commit()
    return JSONResponse(
        content={
            "row_id": row.id,
            "content_hash": snapshot.content_hash,
            "replay_level": snapshot.replay_level,
            "data": data,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/traces/{trace_id}", response_model=list[TraceOut])
def get_trace(
    trace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """按 trace_id 查询完整链路（所有调用，按时间升序回放）。

    新记录直接使用 ``TraceLog.stage``，以保留 workflow/queue/embedding 等生命周期
    事件；仅对历史 stage 为空的记录按旧规则推断 generate/qc。
    """
    rows = (
        scope_query(db.query(TraceLog), TraceLog, current_user)
        .filter(TraceLog.trace_id == trace_id)
        .order_by(TraceLog.created_at.asc())
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
    result: list[TraceOut] = []
    for r in rows:
        out = TraceOut.model_validate(r)
        if not r.stage:
            out.stage = (
                "qc" if (r.output_data and "dimension_scores" in r.output_data) else "generate"
            )
        result.append(out)
    return result


# ---------------------------------------------------------------------------
# 题型模板
# ---------------------------------------------------------------------------
@router.get("/api/templates", response_model=list[TemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("content:read")),
):
    """题型模板列表。"""
    return (
        scope_query(db.query(QuestionTemplate), QuestionTemplate, current_user, shared=True)
        .order_by(QuestionTemplate.type_id)
        .all()
    )


# ---------------------------------------------------------------------------
# 模型档案
# ---------------------------------------------------------------------------
@router.post("/api/model-profiles", response_model=ModelProfileOut)
def create_model_profile(
    req: ModelProfileIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("model:manage")),
):
    """按名称创建或更新模型档案；配置变化写入不可变审计事件。"""
    from app.models import ModelRoute

    if (
        req.status == "disabled"
        and db.query(ModelRoute)
        .filter(
            (ModelRoute.generation_profile == req.name) | (ModelRoute.judge_profile == req.name)
        )
        .first()
    ):
        raise HTTPException(409, "档案仍被题型路由引用，请先清除路由覆盖再禁用")
    if req.is_default and req.status != "enabled":
        raise HTTPException(422, "默认模型必须启用")
    from app.models import ModelProvider

    bound = db.get(ModelProvider, req.provider_id) if req.provider_id else None
    if req.provider_id and (bound is None or bound.status != "enabled"):
        raise HTTPException(422, "所选Provider不存在或已禁用")
    db.flush()
    before_profile = db.query(ModelProfile).filter(ModelProfile.name == req.name).first()
    before = _model_profile_snapshot(before_profile) if before_profile else None
    if req.is_default:
        db.query(ModelProfile).filter(
            ModelProfile.name != req.name, ModelProfile.tenant_id == req.tenant_id
        ).update({ModelProfile.is_default: False}, synchronize_session="fetch")
    if before_profile is None:
        profile = ModelProfile(**req.model_dump())
        db.add(profile)
        action = "create"
    else:
        profile = before_profile
        for key, value in req.model_dump().items():
            setattr(profile, key, value)
        action = "update"
    if bound is not None:
        profile.provider = bound.name
        profile.provider_config_hash = bound.config_hash
    else:
        profile.provider_config_hash = None
    ensure_model_profile_hash(profile)
    try:
        db.flush()
        record_config_change(
            db,
            entity_type="model_profile",
            entity_id=profile.id,
            action=action,
            actor_id=current_user.id,
            tenant_id=profile.tenant_id or current_user.tenant_id,
            before=before,
            after=_model_profile_snapshot(profile),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "模型名称、默认档案或Provider关联冲突，请刷新后重试") from exc
    db.refresh(profile)
    return profile


@router.get("/api/model-profiles", response_model=list[ModelProfileOut])
def list_model_profiles(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("content:read")),
):
    """模型档案列表。"""
    return (
        scope_query(db.query(ModelProfile), ModelProfile, current_user, shared=True)
        .order_by(ModelProfile.name)
        .all()
    )


def _model_profile_snapshot(profile: ModelProfile | None) -> dict | None:
    """返回不含运行时健康计数的模型配置快照。"""
    if profile is None:
        return None
    return {
        "name": profile.name,
        "provider": profile.provider,
        "provider_id": profile.provider_id,
        "provider_config_hash": profile.provider_config_hash,
        "model_name": profile.model_name,
        "model_hash": profile.model_hash,
        "cost_tier": profile.cost_tier,
        "is_default": profile.is_default,
        "status": profile.status,
        "max_fallbacks": profile.max_fallbacks,
        "budget_per_task": profile.budget_per_task,
        "tenant_id": profile.tenant_id,
    }


@router.get("/api/config-audit", response_model=list[ConfigAuditOut])
def list_config_audit(
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    tenant_id: str | None = Query(None),
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """查询模板/模型配置变更；非管理员只能查看自身租户事件。"""
    scoped_tenant = current_user.tenant_id if current_user.role != "admin" else tenant_id
    query = scope_query(db.query(ConfigAuditEvent), ConfigAuditEvent, current_user)
    if entity_type:
        query = query.filter(ConfigAuditEvent.entity_type == entity_type)
    if entity_id:
        query = query.filter(ConfigAuditEvent.entity_id == entity_id)
    if scoped_tenant is not None:
        query = query.filter(ConfigAuditEvent.tenant_id == scoped_tenant)
    if start_at:
        query = query.filter(ConfigAuditEvent.created_at >= start_at)
    if end_at:
        query = query.filter(ConfigAuditEvent.created_at < end_at)
    return query.order_by(ConfigAuditEvent.created_at.desc()).limit(limit).all()


# ---------------------------------------------------------------------------
# 站内消息通知
# ---------------------------------------------------------------------------
@router.get("/api/notifications", response_model=NotificationListOut)
def list_notifications(
    unread_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """站内通知列表（分页），可只看未读，返回未读总数。"""
    service = NotificationService(db)
    skip = (page - 1) * page_size
    result = service.list_notifications(
        current_user, skip=skip, limit=page_size, unread_only=unread_only
    )
    unread = service.get_unread_count(current_user)
    return NotificationListOut(total=result["total"], unread=unread, items=result["items"])


@router.get("/api/notifications/unread-count", response_model=UnreadCountOut)
def unread_notification_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """未读通知数（供前端角标）。"""
    service = NotificationService(db)
    count = service.get_unread_count(current_user)
    return UnreadCountOut(count=count)


@router.post("/api/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记单条通知已读。"""
    service = NotificationService(db)
    result = service.mark_as_read(notification_id, current_user)
    return NotificationOut(**result)


@router.post("/api/notifications/read-all", response_model=UnreadCountOut)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记全部通知已读。"""
    service = NotificationService(db)
    service.mark_all_as_read(current_user)
    return UnreadCountOut(count=0)


# ---------------------------------------------------------------------------
# RAG 知识库
# ---------------------------------------------------------------------------
@router.post("/api/knowledge", response_model=KnowledgeUploadOut)
def upload_knowledge(
    req: KnowledgeUploadIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """上传一份资料（教材/课标/真题）并分块向量化入库。"""
    service = KnowledgeService(db)
    result = service.upload_text(
        text=req.text,
        source_type=req.source_type,
        source_name=req.source_name,
        knowledge_point=req.knowledge_point,
        meta=req.meta,
        user=current_user,
        knowledge_points=req.knowledge_points,
        chunk_layout=req.chunk_layout,
    )
    return KnowledgeUploadOut(**result)


# 上传文件大小上限（10 MB），防止异常大文件拖垮 embedding
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/api/knowledge/upload", response_model=KnowledgeUploadOut)
def upload_knowledge_file(
    file: UploadFile = File(...),
    source_type: str = Form("真题"),
    knowledge_point: str | None = Form(None),
    knowledge_points: str = Form("[]"),
    chunk_layout: Literal["legacy", "structure"] | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """上传教研文档（txt/md/docx/pdf/html/xlsx/csv）并解析、分块向量化入库。

    文件名自动作为 source_name（去扩展名），便于按资料追溯。
    """
    service = KnowledgeService(db)
    # Read at most the limit + 1 before rejecting, not an unbounded read.
    content = file.file.read(_MAX_UPLOAD_BYTES + 1)
    try:
        tags = json.loads(knowledge_points)
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("标签必须是字符串列表")
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="knowledge_points 必须是 JSON 字符串列表"
        ) from exc
    result = service.upload_file(
        file.filename or "", content, source_type, knowledge_point, current_user, tags, chunk_layout
    )
    return KnowledgeUploadOut(**result)


@router.get("/api/knowledge/points")
def knowledge_points_catalog(
    current_user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    from app.rag.knowledge_points import load_catalog

    catalog = load_catalog()
    return {
        "catalog_hash": catalog.hash,
        "defaults": {
            "top_k": settings.RAG_TOP_K,
            "chunk_layout": settings.RAG_CHUNK_LAYOUT,
            "context_mode": settings.RAG_CONTEXT_MODE,
            "context_max_chars": settings.RAG_CONTEXT_MAX_CHARS,
            "scope_mode": settings.RAG_KNOWLEDGE_SCOPE,
            "method": settings.RAG_RETRIEVAL_METHOD,
        },
        "items": [p.model_dump() for p in catalog.points.values() if p.status == "enabled"],
    }


@router.post("/api/knowledge/preview")
def preview_knowledge_file(
    file: UploadFile = File(...),
    chunk_layout: Literal["legacy", "structure"] | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
) -> dict[str, Any]:
    """Preview blocks/chunks/warnings without embedding or database writes."""
    content = file.file.read(_MAX_UPLOAD_BYTES + 1)
    return KnowledgeService(db).preview_file(file.filename or "", content, chunk_layout)


@router.get("/api/knowledge/{chunk_id}/diagnostics")
def knowledge_chunk_diagnostics(
    chunk_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
) -> dict[str, Any]:
    return KnowledgeService(db).chunk_diagnostics(chunk_id, current_user)


@router.get("/api/knowledge", response_model=KnowledgeListOut)
def list_knowledge(
    source_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """知识分块列表（分页，可按资料类型过滤）。"""
    service = KnowledgeService(db)
    result = service.list_knowledge(
        source_type=source_type,
        page=page,
        page_size=page_size,
        user=current_user,
    )
    return KnowledgeListOut(
        total=result["total"],
        items=[KnowledgeChunkOut.model_validate(r) for r in result["items"]],
    )


@router.get("/api/knowledge/documents")
def knowledge_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    return KnowledgeService(db).list_documents(page, page_size, current_user)


@router.get("/api/knowledge/documents/{document_id}/structure")
def knowledge_document_structure(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """Free source/structure preview; no embedding or index mutation."""
    return KnowledgeService(db).structure_preview(document_id, current_user)


@router.post("/api/knowledge/documents/{document_id}/structure-review")
def review_knowledge_document_structure(
    document_id: str,
    body: StructureReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """Free source-bound edge review. Reject stale indexes and unknown edges."""
    return KnowledgeService(db).review_structure(document_id, body, current_user)


@router.delete("/api/knowledge/documents/{document_id}")
def remove_knowledge_document(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    return KnowledgeService(db).delete_document(document_id, current_user)


@router.delete("/api/knowledge/{chunk_id}")
def delete_knowledge(
    chunk_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """删除一条知识分块。"""
    service = KnowledgeService(db)
    result = service.delete_knowledge(chunk_id=chunk_id, user=current_user)
    return result


@router.get("/api/knowledge/retrieve", response_model=KnowledgeRetrieveOut)
def retrieve_knowledge(
    query: str = Query(..., min_length=1, max_length=512),
    knowledge_point: str | None = None,
    top_k: int | None = Query(None, ge=1, le=10),
    scope_mode: Literal["exact", "ancestor", "descendant", "related", "semantic"] | None = None,
    document_ids: List[str] | None = Query(None),
    source_name: str | None = None,
    section_path: List[str] | None = Query(None),
    context_mode: Literal["legacy", "relation"] | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """知识点范围内混合检索、融合与上下文扩展，返回各阶段诊断。"""
    service = KnowledgeService(db)
    result = service.retrieve_knowledge(
        query=query,
        knowledge_point=knowledge_point,
        top_k=top_k,
        user=current_user,
        scope_mode=scope_mode,
        document_ids=document_ids,
        source_name=source_name,
        section_path=section_path,
        context_mode=context_mode,
    )
    return KnowledgeRetrieveOut(**result)


# ---------------------------------------------------------------------------
# 高质量回流样本（数据回流 / J2）
# ---------------------------------------------------------------------------
@router.post("/api/samples", response_model=SamplePoolOut)
def create_sample(
    req: SamplePoolIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """将一条高质量内容沉淀为回流样本（few-shot/微调语料）。"""
    service = SampleService(db)
    sample = service.create_sample_from_content(
        content_id=req.content_id,
        source=req.source,
        purpose=req.purpose,
        user=current_user,
    )
    return sample


@router.post("/api/samples/sync", response_model=SampleSyncOut)
def sync_samples(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """自动将全部高质量内容（人工通过/已发布）沉淀为样本，幂等。"""
    service = SampleService(db)
    result = service.sync_samples(user=current_user)
    return SampleSyncOut(added=result["added"], total=result["total"])


@router.get("/api/samples", response_model=SamplePoolListOut)
def list_samples_api(
    template_id: str | None = Query(None),
    knowledge_point: str | None = Query(None),
    purpose: str | None = Query(None),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """回流样本检索（可按 题型/知识点/用途/来源 过滤，分页）。"""
    service = SampleService(db)
    rows, total = service.list_samples_filtered(
        template_id=template_id,
        knowledge_point=knowledge_point,
        purpose=purpose,
        source=source,
        page=page,
        page_size=page_size,
        user=current_user,
    )
    return SamplePoolListOut(total=total, items=rows)


@router.get("/api/samples/export")
def export_samples(
    template_id: str | None = Query(None),
    knowledge_point: str | None = Query(None),
    purpose: str | None = Query(None),
    source: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:read")),
):
    """导出回流样本为 JSONL（few-shot/微调语料，每行一条样本）。"""
    rows, _ = list_samples(
        db,
        user=current_user,
        template_id=template_id,
        knowledge_point=knowledge_point,
        purpose=purpose,
        source=source,
        page=1,
        page_size=100000,
    )
    lines = []
    for s in rows:
        record = {
            "template_id": s.template_id,
            "source": s.source,
            "purpose": s.purpose,
            "knowledge_point": s.knowledge_point,
            "payload": s.payload,
            "meta": s.meta,
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    filename = "samples.jsonl"
    return PlainTextResponse(
        content="\n".join(lines),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/api/samples/{sample_id}/purpose")
def set_sample_purpose(
    sample_id: str,
    purpose: Literal["sft", "fewshot"],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    from app.models import SamplePool

    row = require_scope(db.get(SamplePool, sample_id), current_user)
    item = require_scope(db.get(ContentItem, row.item_id), current_user)
    if item.status not in {"passed", "published"}:
        raise HTTPException(409, "源内容当前未通过，不能改变样本用途")
    before = {"purpose": row.purpose}
    row.purpose = purpose
    record_config_change(
        db,
        entity_type="sample_pool",
        entity_id=row.id,
        action="purpose",
        actor_id=current_user.id,
        tenant_id=row.tenant_id,
        before=before,
        after={"purpose": purpose},
    )
    db.commit()
    return {"id": row.id, "purpose": row.purpose}


@router.delete("/api/samples/{sample_id}")
def delete_sample(
    sample_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("ops:write")),
):
    """从回流样本库中移除一条样本。"""
    service = SampleService(db)
    result = service.delete_sample_by_id(sample_id=sample_id, user=current_user)
    return result


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@router.get("/api/trace-health")
def trace_health(current_user: User = Depends(require_permission("ops:read"))):
    from app.engine.trace_recovery import trace_diagnostics

    return trace_diagnostics()


@router.get("/api/health", response_model=HealthOut)
def health_check():
    """健康检查。"""
    return HealthOut(status="ok")


@router.get("/api/health/live", response_model=HealthOut)
def health_live():
    """进程存活探针：不访问数据库、Redis 或模型服务。"""
    return HealthOut(status="ok")


@router.get("/api/health/ready", response_model=HealthReportOut)
def health_ready():
    """服务就绪探针：关键依赖不可用时返回 503。"""
    report = readiness_report()
    response = HealthReportOut(**report)
    if not response.ready:
        return JSONResponse(status_code=503, content=response.model_dump())
    return response


@router.get("/api/health/dependencies", response_model=HealthReportOut)
def health_dependencies(
    current_user: User = Depends(require_permission("ops:read")),
):
    """运营依赖详情，仅管理员/教研员可见，响应不包含密钥。"""
    return HealthReportOut(**dependency_report())
