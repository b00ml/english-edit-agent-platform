"""Derived context experiment with extractive LLM proposals; no factual rewrite."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.engine.trace import compute_cost, record_trace, token_breakdown, usage_is_reported
from app.prompt_loader import load_prompt, render
from app.rag.chunking.models import Chunk, ChunkingConfig
from app.rag.document import ParsedDocument, text_hash
from app.rag.retrieval.expansion import _client
from app.versioning import hash_value


class ExtractiveContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    excerpts: list[str] = Field(max_length=3)


def prefixes(
    chunks: list[Chunk],
    document: ParsedDocument,
    cfg: ChunkingConfig,
    *,
    llm: bool = False,
    trace_id: str | None = None,
    max_calls: int = 8,
) -> tuple[list[str], list[dict[str, Any]]]:
    if llm and len(chunks) > max_calls:
        raise ValueError("Context LLM call cap exceeded before calling provider")
    for chunk in chunks:
        minimal = (
            " > ".join(
                dict.fromkeys(x for x in [document["source_name"], *chunk.section_path] if x)
            )
            + "\n\n"
            + chunk.content
        )
        if (
            len(minimal) > cfg.chunk_size
            or cfg.token_limit
            and len(minimal.encode()) > cfg.token_limit
        ):
            raise ValueError(
                "Minimum deterministic context prefix exceeds budget before any LLM call"
            )
    blocks = {b["block_id"]: b for b in document["blocks"]}
    output = []
    metadata = []
    for i, chunk in enumerate(chunks):
        # deterministic, verifiable source title/role prefix; always separate from raw content
        values = [document["source_name"], *chunk.section_path]
        excerpts = []
        if llm:
            source = "\n\n".join(blocks[bid]["text"] for bid in chunk.block_ids)
            source = source[: settings.RAG_EXPERIMENT_CONTEXT_MAX_CHARS]
            system = load_prompt("rag-context-experiment-system.st")
            ut = load_prompt("rag-context-experiment-user.st")
            user = render(ut, {"source": source, "chunk": chunk.content})
            started = time.perf_counter()
            usage = None
            error = None
            raw_response = None
            try:
                response = _client().chat.completions.create(
                    model=settings.RAG_QUERY_EXPANSION_MODEL,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0,
                    max_tokens=settings.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS,
                    response_format={"type": "json_object"},
                )
                raw_response = response.choices[0].message.content
                usage = response.usage
                parsed = ExtractiveContext.model_validate(
                    json.loads(response.choices[0].message.content or "")
                )
                if any(not x.strip() or len(x) > 128 or x not in source for x in parsed.excerpts):
                    raise ValueError("Context proposal is not extractive source evidence")
                excerpts = parsed.excerpts
            except (
                Exception
            ) as exc:  # noqa: BLE001 - record failure then fail closed, never hide paid retries
                error = type(exc).__name__
                raise
            finally:
                tokens = token_breakdown(usage, settings.RAG_QUERY_EXPANSION_MODEL)
                record_trace(
                    snapshot_data={
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "response_text": raw_response,
                        "model": settings.RAG_QUERY_EXPANSION_MODEL,
                        "temperature": 0,
                        "max_tokens": settings.RAG_QUERY_EXPANSION_MAX_OUTPUT_TOKENS,
                        "response_format": {"type": "json_object"},
                    },
                    trace_id=trace_id or "rag-context-experiment:" + uuid.uuid4().hex,
                    model=settings.RAG_QUERY_EXPANSION_MODEL or "unconfigured",
                    stage="rag_context_exp",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    prompt_version=text_hash(system + ut),
                    cost=compute_cost(usage, settings.RAG_QUERY_EXPANSION_MODEL),
                    input_data={
                        "chunk_hash": hash_value(chunk.content),
                        "source_hash": hash_value(source),
                    },
                    output_data={"error_type": error, "excerpts": excerpts},
                    success=error is None,
                    prompt_tokens=tokens["prompt_tokens"],
                    completion_tokens=tokens["completion_tokens"],
                    prompt_cost=tokens["prompt_cost"],
                    completion_cost=tokens["completion_cost"],
                    usage_reported=usage_is_reported(usage, "rag_context_exp"),
                )
        header = " > ".join(dict.fromkeys(x for x in values if x))
        if excerpts:
            header += "\n源文摘录：" + "；".join(excerpts)
        value = header + "\n\n" + chunk.content
        # Reject rather than quietly truncate body or source-derived prefix.
        if len(value) > cfg.chunk_size or cfg.token_limit and len(value.encode()) > cfg.token_limit:
            raise ValueError("Context prefix exceeds full embedding input budget")
        output.append(value)
        metadata.append(
            {
                "chunk_index": i,
                "raw_hash": hash_value(chunk.content),
                "derived_prefix": header,
                "prefix_hash": hash_value(header),
                "excerpts": excerpts,
                "model_derived": llm,
                "verification": "unverified",
            }
        )
    return output, metadata
