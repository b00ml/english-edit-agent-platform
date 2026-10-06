"""WeKnora-inspired chunk contracts; raw content and contextual headers are separate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Strategy = Literal["auto", "heading", "heuristic", "recursive", "legacy"]
CHUNKER_VERSION = "rag-chunker-v2"


@dataclass(frozen=True)
class ChunkingConfig:
    strategy: Strategy = "auto"
    chunk_size: int = 512
    chunk_overlap: int = 80
    token_limit: int | None = None
    separators: tuple[str, ...] = ("\n\n", "\n", "。", ". ", ";", ",", " ")
    preserve_tables: bool = True
    preserve_code_blocks: bool = True
    min_chunk_chars: int = 80
    layout: Literal["legacy", "structure"] = "legacy"

    def normalized(self) -> ChunkingConfig:
        if self.strategy not in {"auto", "heading", "heuristic", "recursive", "legacy"}:
            raise ValueError("未知 chunking strategy")
        if self.layout not in {"legacy", "structure"}:
            raise ValueError("未知 chunk layout")
        if self.chunk_size <= 0 or self.min_chunk_chars <= 0:
            raise ValueError("chunk_size/min_chunk_chars 必须大于 0")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_overlap 必须满足 0 <= chunk_overlap < chunk_size")
        if self.token_limit is not None and self.token_limit <= 0:
            raise ValueError("token_limit 必须大于 0")
        return ChunkingConfig(
            strategy=self.strategy,
            chunk_size=self.chunk_size,
            chunk_overlap=min(self.chunk_overlap, self.chunk_size // 2),
            token_limit=self.token_limit,
            separators=tuple(value for value in self.separators if value),
            preserve_tables=self.preserve_tables,
            preserve_code_blocks=self.preserve_code_blocks,
            min_chunk_chars=min(self.min_chunk_chars, self.chunk_size),
            layout=self.layout,
        )


@dataclass
class Chunk:
    content: str
    start: int
    end: int
    context_header: str = ""
    section_path: list[str] = field(default_factory=list)
    heading_level: int | None = None
    block_ids: list[str] = field(default_factory=list)
    source_locator: list[dict[str, Any]] = field(default_factory=list)
    table_id: str | None = None
    row_range: list[int] | None = None
    page_no: int | None = None
    warning: list[str] = field(default_factory=list)
    hard_split: bool = False
    source_segments: list[dict[str, Any]] = field(default_factory=list)
    logical_unit_id: str | None = None
    chunker_version: str = CHUNKER_VERSION

    @property
    def embedding_content(self) -> str:
        return f"{self.context_header}\n\n{self.content}" if self.context_header else self.content

    def as_meta(self) -> dict[str, Any]:
        return {
            "chunker_version": self.chunker_version,
            "context_header": self.context_header,
            "section_path": self.section_path,
            "heading_level": self.heading_level,
            "block_ids": self.block_ids,
            "source_locator": self.source_locator,
            "table_id": self.table_id,
            "row_range": self.row_range,
            "page_no": self.page_no,
            "content_start": None if self.source_segments else self.start,
            "content_end": None if self.source_segments else self.end,
            "source_envelope": (
                {"start": self.start, "end": self.end, "not_a_contiguous_body_span": True}
                if self.source_segments
                else None
            ),
            "warnings": self.warning,
            "source_segments": self.source_segments,
            "pages": sorted(
                {
                    segment["page_no"]
                    for segment in self.source_segments
                    if segment.get("page_no") is not None
                }
            ),
            "logical_unit_id": self.logical_unit_id,
            "source_range_kind": "segments" if self.source_segments else "contiguous",
        }


@dataclass
class ChunkDiagnostics:
    strategy_requested: str
    strategy_used: str
    chunk_count: int = 0
    min_length: int = 0
    max_length: int = 0
    overlap_actual: int = 0
    coverage_ratio: float = 1.0
    table_chunks: int = 0
    hard_splits: int = 0
    max_embedding_bytes: int = 0
    token_budget_method: str = "utf8_bytes_conservative"
    warnings: list[str] = field(default_factory=list)
    fallback_attempts: list[dict[str, str]] = field(default_factory=list)
    source_coverage_scope: str = "all_source_non_whitespace"
    excluded_navigation_blocks: list[str] = field(default_factory=list)
    layout: str = "legacy"
    plan_signature: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
