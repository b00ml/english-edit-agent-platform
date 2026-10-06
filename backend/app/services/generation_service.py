"""
生成任务 Service
职责：生成任务创建、查询、状态管理
"""

import logging
from typing import Optional

import jsonschema
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dedup import compute_request_hash
from app.engine.router import _resolve_primary_profile_name
from app.errors import DuplicateTaskError, InvalidTemplateParamsError
from app.models import GenerationTask, ModelProfile, QuestionTemplate, User
from app.outbox import create_generation_dispatch, relay_pending
from app.repositories import TaskRepository
from app.schemas import GenerateRequest, GenerateResponse, TaskListOut, TaskOut
from app.tenancy import require_scope, scope_query
from app.versioning import hash_value, task_version_snapshot_with_model


class GenerationService:
    """生成任务业务逻辑层。"""

    def __init__(self, db: Session):
        self.db = db
        self.task_repo = TaskRepository(db)
        self.logger = logging.getLogger("app.services.generation")

    def create_task(self, req: GenerateRequest, current_user: User, celery_app) -> GenerateResponse:
        """创建生成任务并投递到 Celery 队列。

        Args:
            req: 生成请求参数
            current_user: 当前用户
            celery_app: Celery 实例（用于投递任务）

        Returns:
            包含 task_id 的响应

        Raises:
            HTTPException: 题型不存在
            InvalidTemplateParamsError: 参数校验失败
            DuplicateTaskError: 重复提交
        """
        # 校验题型模板存在且未禁用
        template = (
            self.db.query(QuestionTemplate)
            .filter(QuestionTemplate.type_id == req.template_id)
            .first()
        )
        if template is None or template.status == "disabled":
            raise HTTPException(status_code=404, detail="题型模板不存在或已禁用")

        require_scope(template, current_user, shared=True)
        # 校验输入参数符合模板 input_schema（JSON Schema Draft 2020-12）
        if template.input_schema:
            try:
                jsonschema.validate(
                    req.params,
                    template.input_schema,
                    format_checker=jsonschema.FormatChecker(),
                )
            except jsonschema.ValidationError as e:
                # 提取字段路径与校验关键字，脱敏后返回给前端
                field_path = list(e.path) if e.path else []
                validation_keyword = e.validator
                # 错误信息脱敏：只保留字段路径与关键字，不暴露敏感参数值
                raise InvalidTemplateParamsError(
                    message=f"参数校验失败: {e.message}",
                    field_path=field_path,
                    validation_keyword=validation_keyword,
                )

        # 去重：相同题型+规范参数+schema版本且任务仍在执行中，则拒绝重复提交
        # Stable executable template identity, not startup-updated timestamps.
        schema_version = hash_value(
            {
                "version": template.version,
                "template_hash": template.template_hash,
                "output_schema": template.output_schema,
            }
        )
        params = {
            key: value
            for key, value in req.params.items()
            if key not in {"tenant_id", "fewshot_context", "fewshot_provenance"}
        }
        if "quantity" in params:
            params["quantity"] = 1
        from app.engine.providers import route_for_template

        route = route_for_template(self.db, template.type_id)
        route_profile_names = [
            name
            for name in (
                route.generation_profile if route else None,
                route.judge_profile if route else None,
            )
            if name
        ]
        route_profiles = (
            self.db.query(ModelProfile).filter(ModelProfile.name.in_(route_profile_names)).all()
            if route_profile_names
            else []
        )
        request_hash = hash_value(
            {
                "tenant_id": current_user.tenant_id,
                "batch_quantity": req.quantity,
                "model_route": (
                    {"generation": route.generation_profile, "judge": route.judge_profile}
                    if route and (route.generation_profile or route.judge_profile)
                    else None
                ),
                "model_config_hashes": {p.name: p.model_hash for p in route_profiles},
                "request": compute_request_hash(req.template_id, params, schema_version),
            }
        )
        existing = self.task_repo.get_by_request_hash(request_hash)
        if existing is not None:
            raise DuplicateTaskError(
                f"相同题型与参数的生成任务已存在（任务 {existing.id}），请勿重复提交",
                existing.id,
            )

        # 创建任务
        override = route
        model_name = (
            override.generation_profile
            if override and override.generation_profile
            else _resolve_primary_profile_name(template.run_config or {}, params)
        )
        profile = None
        if isinstance(model_name, str):
            profile = (
                scope_query(self.db.query(ModelProfile), ModelProfile, current_user, shared=True)
                .filter(ModelProfile.name == model_name)
                .first()
            )
        if profile is None:
            profile = (
                scope_query(self.db.query(ModelProfile), ModelProfile, current_user, shared=True)
                .filter(ModelProfile.is_default.is_(True))
                .first()
            )
        if profile is not None and not isinstance(profile, ModelProfile):
            profile = None
        snapshot = task_version_snapshot_with_model(template, profile)
        snapshot["model_route"] = (
            {"generation": override.generation_profile, "judge": override.judge_profile}
            if override
            else None
        )
        snapshot["route_profile_hashes"] = {p.name: p.model_hash for p in route_profiles}
        task = self.task_repo.create(
            template_id=req.template_id,
            params=params,
            request_hash=request_hash,
            quantity=req.quantity,
            status="pending",
            progress=0.0,
            user_id=getattr(current_user, "id", None),
            tenant_id=current_user.tenant_id,
            version_snapshot=snapshot,
        )
        try:
            self.db.flush()
            create_generation_dispatch(self.db, task)
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            sqlite_conflict = "generation_task.request_hash" in str(exc.orig)
            if constraint != "uq_active_generation_request" and not sqlite_conflict:
                raise
            winner = (
                self.db.query(GenerationTask)
                .filter(GenerationTask.request_hash == request_hash)
                .order_by(GenerationTask.created_at.desc())
                .first()
            )
            if winner is None:
                raise
            raise DuplicateTaskError(
                "并发提交的相同请求已有任务，请使用已有任务", winner.id
            ) from exc
        self.db.refresh(task)

        # 数据库提交后立即尝试 relay；进程/Redis 故障时保留 pending outbox，
        # 不再留下“任务已创建但消息丢失”的不可解释状态。
        try:
            relay_pending(
                self.db,
                lambda name, args, task_id=None: celery_app.send_task(
                    name, args=args, task_id=task_id
                ),
                limit=1,
            )
        except Exception:  # noqa: BLE001 - relay 失败由 outbox worker 后续补偿
            self.logger.warning("任务 outbox 首次 relay 失败 task_id=%s", task.id, exc_info=True)
        return GenerateResponse(task_id=task.id, status=task.status)

    def get_task(self, task_id: str, current_user: User | None = None) -> GenerationTask:
        """查询单个任务详情与进度。

        Raises:
            HTTPException: 任务不存在
        """
        task = self.task_repo.get_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return require_scope(task, current_user)

    def list_tasks(
        self,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
        current_user: User | None = None,
    ) -> TaskListOut:
        """任务列表（分页）。

        Args:
            page: 页码（从 1 开始）
            page_size: 每页数量
            user_id: 按用户过滤（可选）
            tenant_id: 按租户过滤（可选）
        """
        query = scope_query(self.db.query(GenerationTask), GenerationTask, current_user)
        if tenant_id is not None and (current_user is None or current_user.role == "admin"):
            query = query.filter(GenerationTask.tenant_id == tenant_id)
        total = query.count()
        tasks = (
            query.order_by(GenerationTask.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return TaskListOut(
            total=total,
            page=page,
            page_size=page_size,
            items=[TaskOut.model_validate(task) for task in tasks],
        )

    def update_task_status(
        self, task_id: str, status: str, progress: Optional[float] = None
    ) -> GenerationTask:
        """更新任务状态与进度（供 Worker 调用）。

        Args:
            task_id: 任务 ID
            status: 新状态
            progress: 进度（0-1，可选）
        """
        task = self.task_repo.get_by_id(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        from app.domain.status import TASK_STATUSES
        from app.errors import InvalidGenerationStateError

        if status not in TASK_STATUSES or (progress is not None and not 0 <= progress <= 1):
            raise InvalidGenerationStateError("任务状态或进度不合法")
        update_data = {"status": status}
        if progress is not None:
            update_data["progress"] = progress

        self.task_repo.update(task, **update_data)
        self.db.commit()
        self.db.refresh(task)
        return task
