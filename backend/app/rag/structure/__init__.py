"""Versioned, source-preserving document structure; no models or database writes."""

from app.rag.structure.builder import STRUCTURE_VERSION, build_structure

__all__ = ["STRUCTURE_VERSION", "build_structure"]
