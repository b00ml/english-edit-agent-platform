# app/main.py —— FastAPI 应用入口
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.ocr_routes import router as ocr_router
from app.api.routes import router
from app.config import settings
from app.database import SessionLocal, engine
from app.errors import DuplicateTaskError, PlatformError
from app.model_governance import referenced_profile_names, validate_template_model_references
from app.models import Base, ModelProfile, QuestionTemplate
from app.seed import seed_default_admin
from app.template_loader import load_all_templates
from app.versioning import ensure_model_profile_hash

logger = logging.getLogger("app.main")

# 基础日志配置：结构化输出到 stderr（避免污染流式通道）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _seed_model_profiles(session: Session) -> None:
    """Seed missing profiles only; never overwrite database-administered model mappings."""
    definitions = {"lite": "low", "standard": "standard", "high": "high"}
    for name in settings.MODEL_PROFILE_MODELS:
        definitions.setdefault(name, "standard")
    for name, tier in definitions.items():
        existing = session.query(ModelProfile).filter(ModelProfile.name == name).first()
        if existing is not None:
            ensure_model_profile_hash(existing)
            continue
        profile = ModelProfile(
            name=name,
            provider="configured",
            model_name=settings.MODEL_PROFILE_MODELS.get(name, settings.LLM_MODEL_NAME),
            cost_tier=tier,
            is_default=name == "standard",
        )
        session.add(profile)
        session.flush()
        ensure_model_profile_hash(profile)
    session.commit()


def _verify_migrations(session: Session) -> str:
    """Production bootstrap only accepts the actual Alembic head, not create_all."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    expected: set[str] = set(ScriptDirectory.from_config(config).get_heads())
    current: set[str] = {
        str(row[0])
        for row in session.execute(text("SELECT version_num FROM alembic_version")).all()
    }
    if len(expected) != 1 or current != expected:
        raise RuntimeError("数据库迁移版本与代码 head 不一致，必须先执行 Alembic 迁移")
    return next(iter(current))


def _bootstrap(session: Session) -> None:
    if settings.ENVIRONMENT in {"production", "staging"}:
        _verify_migrations(session)
    expected = len(list((Path(__file__).parent / "templates").glob("*.yaml")))
    loaded = load_all_templates(session)
    if loaded != expected or loaded == 0:
        raise RuntimeError("题型模板未完整加载，拒绝启动")
    _seed_model_profiles(session)
    errors = validate_template_model_references(session)
    if errors:
        raise RuntimeError("; ".join(errors))
    mappings = {
        name: model
        for name, model in session.query(ModelProfile.name, ModelProfile.model_name).all()
    }
    primary = {mappings.get(name) for name in ("lite", "standard", "high")}
    if len(primary) < 3:
        if settings.REQUIRE_DISTINCT_MODEL_TIERS:
            raise RuntimeError("配置要求独立模型分档，但实际档案仍共用模型")
        logger.warning("模型档案分档尚未独立配置；不代表不同能力档位")
    judge = settings.JUDGE_MODEL_NAME or settings.LLM_MODEL_NAME
    if settings.REQUIRE_INDEPENDENT_JUDGE and judge in primary:
        raise RuntimeError("配置要求独立 Judge，但 Judge 与生成模型相同")
    if settings.REQUIRE_INDEPENDENT_JUDGE:
        for template in (
            session.query(QuestionTemplate).filter(QuestionTemplate.status == "enabled").all()
        ):
            config = template.run_config or {}
            actual_judge = config.get("judge_model") or judge
            if actual_judge in {mappings.get(name) for name in referenced_profile_names(config)}:
                raise RuntimeError("模板 Judge 与该模板生成模型相同，拒绝独立评审声明")
    seed_default_admin(session)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.ENVIRONMENT not in {"production", "staging"}:
        Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        _bootstrap(session)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - rollback critical initialization and propagate failure
        session.rollback()
        logger.error("关键启动步骤失败 error_type=%s", type(exc).__name__)
        raise
    finally:
        session.close()
    yield


def create_app() -> FastAPI:
    """构建 FastAPI 应用。"""
    app = FastAPI(
        title="英语教研 AI 内容生成平台 2.0",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS 配置（允许前端开发服务器跨域访问）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Never echo rejected request inputs: credentials may occur in any nested field.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    # 全局异常处理：PlatformError -> 结构化错误响应（含错误码）
    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError):
        content: dict[str, object] = {"code": exc.code, "detail": exc.message}
        if isinstance(exc, DuplicateTaskError):
            content["existing_task_id"] = exc.existing_task_id
        return JSONResponse(status_code=exc.status_code, content=content)

    # 兜底异常处理：未捕获异常统一返回 500，避免暴露堆栈
    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("未处理异常 %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "detail": "服务器内部错误"},
        )

    from app.api.model_settings import router as model_settings_router

    app.include_router(model_settings_router)
    app.include_router(ocr_router)
    app.include_router(router)
    return app


app = create_app()
