"""Stable ID-based, one-rank-per-lane RRF, adapted from WeKnora search fusion."""

from __future__ import annotations

from app.rag.retrieval.models import Candidate


def fuse(lanes: dict[str, list[Candidate]], k: int) -> list[Candidate]:
    if k < 1:
        raise ValueError("RRF k 必须大于 0")
    merged: dict[str, Candidate] = {}
    for lane, candidates in lanes.items():
        seen: set[str] = set()
        for rank, candidate in enumerate(candidates, 1):
            key = candidate.row.id
            if key in seen:
                continue
            seen.add(key)
            result = merged.setdefault(key, Candidate(candidate.row))
            result.ranks[lane] = rank
            result.rrf_score += 1.0 / (k + rank)
            if candidate.similarity is not None:
                result.similarity = max(
                    result.similarity if result.similarity is not None else -1, candidate.similarity
                )
            if candidate.keyword_score is not None:
                result.keyword_score = max(result.keyword_score or 0, candidate.keyword_score)
    return sorted(merged.values(), key=lambda c: (-c.rrf_score, c.row.id))
