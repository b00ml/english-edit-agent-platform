"""Public chunking interface; preserves legacy imports."""

from app.rag.chunking.models import CHUNKER_VERSION, Chunk, ChunkDiagnostics, ChunkingConfig
from app.rag.chunking.strategy import split_document, split_text

__all__ = [
    "CHUNKER_VERSION",
    "Chunk",
    "ChunkDiagnostics",
    "ChunkingConfig",
    "split_document",
    "split_text",
]
