"""PostgreSQL FTS + bounded CJK bigram/literal recall, not BM25 or segmentation."""

from __future__ import annotations

import re

from sqlalchemy import and_, case, func, literal, literal_column, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.config import settings
from app.models import KnowledgeChunk
from app.rag.knowledge_points import KnowledgeScope, normalized
from app.rag.retrieval.models import (
    Candidate,
    dialect_name,
    leaf_predicate,
    predicates,
    search_text,
)


def terms(query: str) -> list[str]:
    whole = normalized(query)
    if not whole:
        return []
    fragments = re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*|[\u3400-\u9fff]+|[-+][a-z]+", whole)
    # Keep the rollback path byte-for-byte equivalent to the old token policy.
    base = list(dict.fromkeys([whole, *fragments]))
    if settings.RAG_CJK_KEYWORD_MODE == "legacy":
        return base[:16]
    # The English FTS parser does not segment continuous Chinese questions. Bigrams
    # restore a lexical lane without a model, a domain dictionary or scope rewriting.
    pairs = list(
        dict.fromkeys(
            run[index : index + 2]
            for run in re.findall(r"[\u3400-\u9fff]+", whole)
            for index in range(len(run) - 1)
        )
    )
    limit = settings.RAG_KEYWORD_MAX_TERMS
    if len(base) >= limit:
        # Retain full phrase plus samples across a long mixed-language question.
        return [base[index * (len(base) - 1) // (limit - 1)] for index in range(limit)]
    remaining = [value for value in pairs if value not in base]
    slots = limit - len(base)
    if len(remaining) > slots:
        # Avoid truncating every useful term at the end of a long question.
        remaining = (
            [remaining[len(remaining) // 2]]
            if slots == 1
            else [remaining[i * (len(remaining) - 1) // (slots - 1)] for i in range(slots)]
        )
    return [*base, *remaining]


def recall(
    session: Session,
    query: str,
    scope: KnowledgeScope,
    tenant: str | None,
    allow_all: bool,
    pool: int,
    document_ids: list[str] | None = None,
) -> list[Candidate]:
    keywords = terms(query)
    if not keywords:
        return []
    text = func.coalesce(
        KnowledgeChunk.search_text,
        func.lower(
            func.coalesce(KnowledgeChunk.source_name, "")
            + " "
            + func.coalesce(KnowledgeChunk.knowledge_point, "")
            + " "
            + func.coalesce(KnowledgeChunk.context_header, "")
            + " "
            + KnowledgeChunk.content
        ),
    )
    literal_matches = [
        or_(
            KnowledgeChunk.search_text.contains(term, autoescape=True),
            and_(KnowledgeChunk.search_text.is_(None), text.contains(term, autoescape=True)),
        )
        for term in keywords
    ]
    literal_score = sum((case((match, 1.0), else_=0.0) for match in literal_matches), literal(0.0))
    filters = [*predicates(scope, tenant, allow_all, session, document_ids), leaf_predicate()]
    if dialect_name(session) == "postgresql":
        configuration: ColumnElement[str] = literal_column("'english'::regconfig")
        ts_vector = func.to_tsvector(configuration, func.coalesce(KnowledgeChunk.search_text, ""))
        ts_query = func.websearch_to_tsquery(configuration, query)
        match = ts_vector.op("@@")(ts_query)
        rank = func.ts_rank_cd(ts_vector, ts_query)
        filters.append(or_(match, *literal_matches))
        stmt = (
            select(KnowledgeChunk)
            .where(*filters)
            .order_by((rank + literal_score).desc(), KnowledgeChunk.id)
            .limit(pool)
        )
    else:
        filters.append(or_(*literal_matches))
        stmt = (
            select(KnowledgeChunk)
            .where(*filters)
            .order_by(literal_score.desc(), KnowledgeChunk.id)
            .limit(pool)
        )
    rows = session.execute(stmt).scalars().all()
    result = []
    for index, row in enumerate(rows, 1):
        # PostgreSQL has already checked FTS/literal match. Preserve its rank,
        # even when an inflected word matched only via FTS stemming.
        if dialect_name(session) == "sqlite" and not any(
            term in search_text(row) for term in keywords
        ):
            continue
        result.append(Candidate(row, keyword_score=1.0 / index))
    return result
