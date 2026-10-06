# app/rag/embedding.py —— 文本向量化
# 调用 OpenAI 兼容 embedding 端点（阿里云百炼 text-embedding-v3）生成向量。
import logging
import time
import uuid
from typing import List

from openai import OpenAI

from app.config import settings
from app.engine.trace import (
    compute_cost,
    elapsed_ms,
    record_trace,
    token_breakdown,
    usage_is_reported,
)
from app.errors import InvalidKnowledgeError

logger = logging.getLogger("app.rag.embedding")


def _get_openai_client() -> OpenAI:
    """构建 embedding 客户端（可测试缝：单测/集成测试可整体替换本函数）。"""
    return OpenAI(
        base_url=settings.EMBEDDING_API_BASE,
        api_key=settings.EMBEDDING_API_KEY or "sk-placeholder",
        timeout=settings.EMBEDDING_TIMEOUT,
    )


def embed_texts(
    texts: List[str],
    *,
    trace_id: str | None = None,
    task_id: str | None = None,
    template_id: str | None = None,
    tenant_id: str | None = None,
) -> List[List[float]]:
    """批量将文本转换为向量（保留输入顺序）。"""
    if not texts:
        return []
    effective_trace_id = trace_id or f"embedding:{uuid.uuid4()}"
    started = time.perf_counter_ns()
    client = _get_openai_client()
    try:
        resp = client.embeddings.create(
            model=settings.EMBEDDING_MODEL_NAME,
            input=texts,
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - record provider failure then preserve caller semantics
        record_trace(
            trace_id=effective_trace_id,
            task_id=task_id,
            template_id=template_id,
            tenant_id=tenant_id,
            model=settings.EMBEDDING_MODEL_NAME,
            latency_ms=elapsed_ms(started),
            cost=0.0,
            input_data={"text_count": len(texts)},
            output_data={"error_summary": str(exc)[:200]},
            stage="embedding",
            success=False,
        )
        raise
    usage = getattr(resp, "usage", None)
    tokens = token_breakdown(usage, settings.EMBEDDING_MODEL_NAME)
    failure: InvalidKnowledgeError | None = None
    vectors: List[List[float]] = []
    try:
        indices = [item.index for item in resp.data]
        if sorted(indices) != list(range(len(texts))):
            raise InvalidKnowledgeError("Embedding 返回索引缺失、重复或越界")
        vectors = [item.embedding for item in sorted(resp.data, key=lambda item: item.index)]
    except (AttributeError, TypeError, InvalidKnowledgeError) as exc:
        failure = InvalidKnowledgeError("Embedding 返回数据不完整")
        failure.__cause__ = exc
    record_trace(
        trace_id=effective_trace_id,
        task_id=task_id,
        template_id=template_id,
        tenant_id=tenant_id,
        model=settings.EMBEDDING_MODEL_NAME,
        latency_ms=elapsed_ms(started),
        cost=compute_cost(usage, settings.EMBEDDING_MODEL_NAME),
        input_data={"text_count": len(texts)},
        output_data=(
            {"error_type": type(failure).__name__}
            if failure is not None
            else {
                "text_count": len(vectors),
                "dimension": len(vectors[0]) if vectors else 0,
                "cost_basis": (
                    "configured_model_rate"
                    if settings.EMBEDDING_MODEL_NAME in settings.MODEL_PRICES
                    else "global_fallback_unverified"
                ),
                "provider_bill_verified": False,
            }
        ),
        stage="embedding",
        success=failure is None,
        prompt_tokens=tokens["prompt_tokens"],
        completion_tokens=tokens["completion_tokens"],
        usage_reported=usage_is_reported(usage, "embedding"),
        prompt_cost=tokens["prompt_cost"],
        completion_cost=tokens["completion_cost"],
    )
    if failure is not None:
        raise failure
    return vectors
