"""Vector recall with model/dimension safety and a labelled SQLite test reference path."""

from __future__ import annotations

import math

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import InvalidKnowledgeError
from app.models import KnowledgeChunk
from app.rag.knowledge_points import KnowledgeScope
from app.rag.retrieval.models import Candidate, dialect_name, leaf_predicate, predicates


def cosine(left: list[float], right: list[float] | None) -> float | None:
    if right is None or len(left) != len(right) or not all(math.isfinite(float(v)) for v in right):
        return None
    denominator = math.sqrt(sum(v * v for v in left) * sum(float(v) ** 2 for v in right))
    return sum(a * float(b) for a, b in zip(left, right)) / denominator if denominator else None


def recall(
    session: Session,
    vector: list[float],
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
    pool: int,
    document_ids: list[str] | None = None,
) -> list[Candidate]:
    if (
        len(vector) != settings.EMBEDDING_DIM
        or not all(math.isfinite(v) for v in vector)
        or not any(vector)
    ):
        raise InvalidKnowledgeError("查询向量维度/数值无效")
    filters = predicates(scope, tenant, allow_all, session, document_ids)
    filters.extend(
        [
            leaf_predicate(),
            KnowledgeChunk.embedding.is_not(None),
            or_(
                KnowledgeChunk.embedding_model.is_(None),
                KnowledgeChunk.embedding_model == settings.EMBEDDING_MODEL_NAME,
            ),
            or_(
                KnowledgeChunk.embedding_dimension.is_(None),
                KnowledgeChunk.embedding_dimension == settings.EMBEDDING_DIM,
            ),
        ]
    )
    stmt = select(KnowledgeChunk).where(*filters)
    if dialect_name(session) == "sqlite":
        # Offline reference ONLY; production uses PostgreSQL ordering/HNSW.
        rows = session.execute(stmt.limit(5000)).scalars().all()
        candidates = [Candidate(row, similarity=cosine(vector, row.embedding)) for row in rows]
        candidates.sort(
            key=lambda c: (-(c.similarity if c.similarity is not None else -2), c.row.id)
        )
    else:
        stmt = stmt.order_by(
            KnowledgeChunk.embedding.cosine_distance(vector), KnowledgeChunk.id
        ).limit(pool)
        candidates = [
            Candidate(row, similarity=cosine(vector, row.embedding))
            for row in session.execute(stmt).scalars().all()
        ]
    return [
        c
        for c in candidates
        if c.similarity is not None and c.similarity >= settings.RAG_MIN_SIMILARITY
    ][:pool]
