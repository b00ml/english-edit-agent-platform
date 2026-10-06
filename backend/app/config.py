# app/config.py —— 全局配置（Pydantic Settings）
# 从环境变量（或 .env）读取平台运行配置，带类型化默认值，未设置时用默认值。
import json
import math
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _is_placeholder(value: str) -> bool:
    """识别示例文件中的空值和显式占位值。"""
    normalized = value.strip().lower()
    return (
        not normalized
        or normalized.startswith("replace-with-")
        or normalized
        in {
            "sk-xxxxxxxx",
            "your-key",
            "your-secret",
        }
    )


class Settings(BaseSettings):
    """应用配置集合。

    所有字段均可由同名环境变量覆盖；int/float 字段自动做类型转换。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Dedicated Fernet key for provider credentials; blank disables UI credential writes.
    PROVIDER_SECRET_KEY: str = ""

    # 数据库连接串（SQLAlchemy 格式）
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/english_edit"

    # LangGraph checkpointer 后端：postgres（持久化断点，跨重启续跑）/ memory（进程内）。
    # postgres 初始化失败时自动降级 memory 并告警，保证可用性优先。
    CHECKPOINTER_BACKEND: str = "postgres"
    # 仅允许在 staging/production 显式放行 MemorySaver 降级；默认保持 not-ready。
    ALLOW_MEMORY_CHECKPOINTER: bool = False
    # Redis 连接串（数据队列 / Cache）
    REDIS_URL: str = "redis://localhost:6379/0"

    # Langfuse 可观测性配置（用于追踪 LLM 调用，可为空则关闭）
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # Trace 分发目标（逗号分隔）：db=自研 TraceLog 表（SSOT）；langfuse=可选导出通道。
    # 任一 sink 写入失败均不影响其余 sink 与业务主流程。
    TRACE_SINKS: str = "db"
    TRACE_FAILURE_SPOOL_DIR: str = ""
    TRACE_REQUIRE_DURABILITY: bool = False
    TRACE_SNAPSHOT_ENABLED: bool = False
    TRACE_SNAPSHOT_SECRET_KEY: str = ""
    TRACE_SNAPSHOT_MAX_BYTES: int = Field(default=1048576, ge=4096, le=8388608)
    TRACE_SNAPSHOT_TOTAL_BYTES: int = Field(default=134217728, ge=1048576, le=1073741824)
    TRACE_SNAPSHOT_RETENTION_DAYS: int = Field(default=7, ge=1, le=90)
    FEWSHOT_ENABLED: bool = False
    FEWSHOT_MAX_EXAMPLES: int = Field(default=2, ge=0, le=5)
    FEWSHOT_MAX_CHARS: int = Field(default=8192, ge=256, le=32768)
    FEWSHOT_CANDIDATE_LIMIT: int = Field(default=50, ge=1, le=200)

    # OpenAI 兼容的云 API 配置
    LLM_API_BASE: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    # 默认模型名（无模型档案或档案缺失时兜底使用）
    LLM_MODEL_NAME: str = "deepseek-v4-flash"
    # judge 质检模型名（自偏好偏差治理：建议配置为与生成主模型不同家族的模型）。
    # 解析优先级：模板 run_config.judge_model > JUDGE_MODEL_NAME > LLM_MODEL_NAME。
    JUDGE_MODEL_NAME: str = ""
    JUDGE_API_BASE: str = ""
    JUDGE_API_KEY: str = ""
    MODEL_PROFILE_MODELS: Annotated[dict[str, str], NoDecode] = {}
    REQUIRE_DISTINCT_MODEL_TIERS: bool = False
    REQUIRE_INDEPENDENT_JUDGE: bool = False
    # 生成/judge 采样温度（此前硬编码于引擎，配置化对齐"全配置驱动"口径）
    LLM_TEMPERATURE: float = 0.7
    JUDGE_TEMPERATURE: float = 0.3
    # 分模型价目表（每 1K token 单价）：{"模型名": {"prompt": x, "completion": y}}。
    # 未命中的模型回退 COST_PER_1K_TOKENS 单一单价（历史数据口径不变）。
    MODEL_PRICES: dict[str, Any] = (
        {}
    )  # Dynamic provider price-map boundary; unchanged runtime shape.
    # 单次 LLM 请求超时（秒）。显式设置而非依赖 SDK 默认 600s，避免连接挂起时单请求
    # 阻塞过久；超时抛 APITimeoutError，经 router 降级兜底处理。
    LLM_TIMEOUT: int = 120
    # 单次 embedding 请求超时（秒）
    EMBEDDING_TIMEOUT: int = 30

    # 结构化输出重试时是否回注校验错误信息（关闭则保持原样重发，兼容旧行为）
    STRUCTURED_RETRY_FEEDBACK: bool = True

    # Celery 任务可靠性（P0-6）
    # 任务级最大重试次数（仅瞬态基础设施错误：redis 连接/超时、DB OperationalError）
    TASK_MAX_RETRIES: int = 3
    # running 状态超过该秒数判定为僵尸任务，worker 启动时重置为 pending 并重新入队
    STALE_TASK_SECONDS: int = 1800

    # Outbox relay（数据库事务提交后投递 Celery）
    OUTBOX_MAX_ATTEMPTS: int = 5
    OUTBOX_RELAY_BATCH_SIZE: int = 50
    OUTBOX_RETRY_BACKOFF_SECONDS: int = 5
    # relay 领取到事件后进程异常时，超过该时间可重新领取；避免 sending 永久滞留。
    OUTBOX_SENDING_TIMEOUT_SECONDS: int = 300
    GENERATION_MAINTENANCE_SECONDS: int = Field(default=20, ge=5, le=3600)
    GENERATION_RECONCILE_BATCH_SIZE: int = Field(default=50, ge=1, le=500)
    GENERATION_DELIVERY_TIMEOUT_SECONDS: int = Field(default=3600, ge=60)
    GENERATION_STALE_ITEM_SECONDS: int = Field(default=1800, ge=960)

    # 模型运行时治理（P2-1）；预算为 0 表示不启用全局上限。
    MODEL_COOLDOWN_SECONDS: int = 60
    MODEL_FAILURE_THRESHOLD: int = 2
    MODEL_MAX_FALLBACKS: int = 1
    MODEL_BUDGET_PER_TASK: float = 0.0

    # 成本估算单价（每 1000 token），用于记录 TraceLog.cost
    COST_PER_1K_TOKENS: float = 0.002

    # 质检 LLM 采样次数（降低抖动；rubric 已使单次稳定，默认 1，需降噪时调高）
    JUDGE_SAMPLE_ROUNDS: int = 1
    # 每轮 Judge 评分的最大尝试次数（包括首次），无效输出耗尽后失败关闭。
    JUDGE_MAX_RETRIES: int = 3

    # 人工质检默认阈值
    QUALITY_THRESHOLD: float = 70.0

    # RAG 向量检索：OpenAI 兼容 embedding 端点（阿里云百炼 text-embedding-v3）
    EMBEDDING_API_BASE: str = "https://your-embedding-provider.example/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL_NAME: str = "text-embedding-v3"
    # 向量维度（与 text-embedding-v3 默认输出一致）
    EMBEDDING_DIM: int = 1024
    RAG_MODE: str = "optional"
    RAG_MIN_SIMILARITY: float = 0.3
    # Applies only to new ingestion; persisted legacy chunks are never re-embedded implicitly.
    RAG_CHUNK_STRATEGY: Literal["auto", "heading", "heuristic", "recursive", "legacy"] = "auto"
    RAG_EXPERIMENT_MAX_CHUNKS: int = Field(default=128, ge=1, le=512)
    RAG_EXPERIMENT_MAX_SENTENCES: int = Field(default=128, ge=2, le=1024)
    RAG_EXPERIMENT_SEMANTIC_THRESHOLD: float = Field(default=0.65, ge=-1, le=1)
    RAG_EXPERIMENT_CONTEXT_MAX_CHARS: int = Field(default=4096, ge=128, le=16384)
    RAG_EXPERIMENT_MAX_LLM_CALLS: int = Field(default=8, ge=1, le=32)
    RAG_EXPERIMENT_MAX_TOKENS: int = Field(default=8192, ge=32, le=16384)
    RAG_EXPERIMENT_MAX_INPUT_BYTES: int = Field(default=4194304, ge=1024, le=16777216)
    RAG_CHUNK_LAYOUT: Literal["legacy", "structure"] = "structure"
    RAG_CHUNK_SIZE: int = 512
    RAG_CHUNK_OVERLAP: int = 80
    RAG_CHUNK_MIN_CHARS: int = 80
    RAG_CHUNK_TOKEN_LIMIT: int | None = None
    RAG_EMBEDDING_BATCH_SIZE: int = 32
    RAG_PARENT_CHILD_ENABLED: bool = True
    RAG_PARENT_MIN_CHARS: int = 1024
    RAG_PARENT_SIZE: int = 2048
    RAG_NEIGHBOR_WINDOW: int = 1
    RAG_CONTEXT_MAX_CHARS: int = 32768
    RAG_CONTEXT_TOKEN_LIMIT: int | None = None
    RAG_CONTEXT_EXPANSION_LIMIT: int = 24
    # Preserve the v1 route by default; deployed environment/frontend can select relation.
    RAG_CONTEXT_MODE: Literal["legacy", "relation"] = "relation"
    RAG_STRUCTURE_ENABLED: bool = True
    RAG_QUERY_PLANNING_MODE: Literal["off", "rules", "llm"] = "rules"
    RAG_QUERY_PLAN_MAX_PARTS: int = Field(default=4, ge=1, le=4)
    RAG_QUERY_PLAN_MAX_QUERIES: int = Field(default=8, ge=2, le=8)
    RAG_OCR_BOUNDARY_TIMEOUT: int = Field(default=360, ge=30, le=1800)
    RAG_OCR_BOUNDARY_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=5)
    RAG_STRUCTURE_MAX_BLOCKS: int = Field(default=10000, ge=1, le=20000)
    RAG_STRUCTURE_MAX_LEAFS: int = Field(default=2000, ge=1, le=10000)
    RAG_CONTEXT_BUNDLE_MAX_MEMBERS: int = Field(default=24, ge=1, le=128)
    RAG_CONTEXT_BUNDLE_MAX_HOPS: int = Field(default=32, ge=1, le=128)
    RAG_CONTEXT_MAX_SEGMENTS: int = Field(default=128, ge=1, le=512)
    RAG_CANDIDATE_POOL: int = 30
    RAG_TOP_K: int = 5
    RAG_RETRIEVAL_METHOD: Literal["vector", "hybrid"] = "hybrid"
    RAG_RRF_K: int = 60
    RAG_KEYWORD_MIN_SCORE: float = 0.001
    RAG_CJK_KEYWORD_MODE: Literal["legacy", "bigram"] = "bigram"
    RAG_KEYWORD_MAX_TERMS: int = Field(default=32, ge=2, le=64)
    RAG_RERANK_MODE: Literal["off", "optional", "required"] = "off"
    RAG_RERANK_URL: str = ""
    RAG_RERANK_API_KEY: str = ""
    RAG_RERANK_MODEL: str = ""
    RAG_RERANK_TIMEOUT: float = 10.0
    RAG_RERANK_THRESHOLD: float = 0.0
    RAG_KNOWLEDGE_CATALOG: str = ""
    RAG_KNOWLEDGE_SCOPE: Literal["exact", "ancestor", "descendant", "related", "semantic"] = "exact"
    RAG_QUERY_EXPANSION_MODE: Literal["off", "aliases", "llm"] = "aliases"
    RAG_KNOWLEDGE_SCOPE_MAX: int = 128
    RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS: int = 512
    RAG_QUERY_EXPANSION_MAX: int = 4
    RAG_QUERY_EXPANSION_MODEL: str = ""
    RAG_QUERY_EXPANSION_API_BASE: str = ""
    RAG_QUERY_EXPANSION_API_KEY: str = ""
    RAG_QUERY_EXPANSION_TIMEOUT: float = 8.0

    # OCR is an explicit offline/background operation; ordinary HTTP preview never runs it.
    RAG_OCR_ENGINE: Literal["off", "mineru"] = "off"
    RAG_OCR_URL: str = ""
    RAG_OCR_API_KEY: str = ""
    RAG_OCR_PAGE_TIMEOUT: float = Field(default=180.0, gt=0, le=600)
    RAG_OCR_MAX_PAGES: int = Field(default=3, ge=1, le=100)
    RAG_OCR_MAX_INPUT_BYTES: int = Field(default=134217728, ge=1, le=536870912)
    RAG_OCR_MAX_RESULT_BYTES: int = Field(default=4194304, ge=1024, le=16777216)
    RAG_PDF_MIN_TEXT_CHARS: int = Field(default=10, ge=1)
    RAG_PDF_SCAN_IMAGE_RATIO: float = Field(default=0.5, gt=0, le=1)

    RAG_OCR_STORAGE_DIR: str = ".local-ocr"
    RAG_OCR_IMPORT_ROOT: str = ""
    RAG_OCR_MAX_JOB_PAGES: int = Field(default=500, ge=1, le=2000)
    RAG_OCR_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    RAG_OCR_RETRY_SECONDS: int = Field(default=10, ge=1, le=3600)
    RAG_OCR_LEASE_SECONDS: int = Field(default=360, ge=30, le=3600)
    RAG_OCR_SWEEP_SECONDS: int = Field(default=20, ge=5, le=300)
    RAG_OCR_MAX_PREVIEW_BYTES: int = Field(default=67108864, ge=1024, le=268435456)
    RAG_OCR_CACHE_REVISION: str = "mineru-middle2-adapter-v1"
    RAG_OCR_INDEX_TIMEOUT: int = Field(default=900, ge=60, le=3600)
    RAG_OCR_INDEX_MAX_CHUNKS: int = Field(default=1000, ge=1, le=10000)
    EMBEDDING_PROVIDER_BATCH_LIMIT: int = Field(default=10, ge=1, le=2048)

    # JWT 认证（K4 权限）
    # 生产环境务必通过环境变量覆盖默认密钥
    JWT_SECRET: str = "change-me-english-edit-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    # token 有效期（秒），默认 12 小时
    JWT_EXPIRE_SECONDS: int = 43200

    # 种子默认管理员账号（首次启动、用户表为空时自动写入；生产环境务必覆盖默认密码）
    SEED_ADMIN_USERNAME: str = "admin"
    SEED_ADMIN_PASSWORD: str = "admin123"
    SEED_ADMIN_DISPLAY_NAME: str = "系统管理员"

    # 环境标识（P0-5 生产环境 fail-fast）
    ENVIRONMENT: str = "development"
    CORS_ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    CORS_ALLOW_CREDENTIALS: bool = False

    @field_validator("RAG_CHUNK_TOKEN_LIMIT", "RAG_CONTEXT_TOKEN_LIMIT", mode="before")
    @classmethod
    def parse_optional_rag_limit(cls, value: object) -> object:
        """Compose renders unset optional settings as empty strings, not Python None."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("MODEL_PROFILE_MODELS", mode="before")
    @classmethod
    def parse_profile_models(cls, value: object) -> object:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else {}
        if isinstance(value, dict) and any(
            not isinstance(v, str) or not v.strip() for v in value.values()
        ):
            raise ValueError("模型档案映射必须提供非空模型名")
        return value

    def __init__(self, **kwargs: Any) -> None:  # Pydantic Settings environment/input boundary.
        super().__init__(**kwargs)
        if "*" in self.CORS_ALLOWED_ORIGINS and (
            self.CORS_ALLOW_CREDENTIALS or self.ENVIRONMENT in {"production", "staging"}
        ):
            raise ValueError("CORS 通配符不允许用于凭据请求或生产/预发环境")
        if not -1 <= self.RAG_MIN_SIMILARITY <= 1 or self.RAG_MODE not in {
            "off",
            "optional",
            "required",
        }:
            raise ValueError("RAG 配置无效")
        if (
            self.RAG_CHUNK_SIZE <= 0
            or not 0 <= self.RAG_CHUNK_OVERLAP < self.RAG_CHUNK_SIZE
            or self.RAG_CHUNK_MIN_CHARS <= 0
            or self.RAG_EMBEDDING_BATCH_SIZE <= 0
            or (self.RAG_CHUNK_TOKEN_LIMIT is not None and self.RAG_CHUNK_TOKEN_LIMIT <= 0)
        ):
            raise ValueError("RAG 切块/批量索引配置无效")
        if (
            (self.RAG_PARENT_CHILD_ENABLED and self.RAG_PARENT_SIZE < self.RAG_CHUNK_SIZE)
            or self.RAG_PARENT_MIN_CHARS < 1
            or not 0 <= self.RAG_NEIGHBOR_WINDOW <= 3
            or self.RAG_CONTEXT_MAX_CHARS < 64
            or self.RAG_CONTEXT_EXPANSION_LIMIT < 1
            or (self.RAG_CONTEXT_TOKEN_LIMIT is not None and self.RAG_CONTEXT_TOKEN_LIMIT < 64)
            or not 1 <= self.RAG_TOP_K <= min(10, self.RAG_CANDIDATE_POOL)
            or not 1 <= self.RAG_CANDIDATE_POOL <= 100
            or self.RAG_RRF_K < 1
            or self.RAG_KEYWORD_MIN_SCORE < 0
            or not 1 <= self.RAG_KNOWLEDGE_SCOPE_MAX <= 256
            or not 32 <= self.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS <= 2048
            or not 1 <= self.RAG_QUERY_EXPANSION_MAX <= 8
            or self.RAG_RERANK_TIMEOUT <= 0
            or self.RAG_QUERY_EXPANSION_TIMEOUT <= 0
            or not all(
                math.isfinite(v)
                for v in [
                    self.RAG_RERANK_THRESHOLD,
                    self.RAG_KEYWORD_MIN_SCORE,
                    self.RAG_RERANK_TIMEOUT,
                    self.RAG_QUERY_EXPANSION_TIMEOUT,
                ]
            )
        ):
            raise ValueError("RAG P1 上下文/检索配置无效")
        # P0-5 生产环境强制密钥检查
        if self.ENVIRONMENT == "production":
            if self.JWT_SECRET == "change-me-english-edit-jwt-secret" or _is_placeholder(
                self.JWT_SECRET
            ):
                raise ValueError("生产环境必须设置非占位 JWT_SECRET")
            if len(self.JWT_SECRET.encode("utf-8")) < 32:
                raise ValueError("生产环境 JWT_SECRET 至少需要 32 字节")
            if self.SEED_ADMIN_PASSWORD == "admin123" or _is_placeholder(self.SEED_ADMIN_PASSWORD):
                raise ValueError("生产环境必须设置非占位 SEED_ADMIN_PASSWORD")
            if _is_placeholder(self.LLM_API_KEY):
                raise ValueError("生产环境必须设置非占位 LLM_API_KEY")
            if _is_placeholder(self.EMBEDDING_API_KEY):
                raise ValueError("生产环境必须设置非占位 EMBEDDING_API_KEY")


settings = Settings()
