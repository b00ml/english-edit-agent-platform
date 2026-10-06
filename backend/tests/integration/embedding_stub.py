"""Deterministic non-zero, non-identical vectors for unpaid integration tests.

Token hashing is a test double, not a semantic embedding model or quality proof.
Dimension one has a small positive bias so even empty input is never the zero vector.
"""

from __future__ import annotations

import hashlib
import math
import re


def mock_vector(text: str, dimension: int = 1024) -> list[float]:
    if dimension < 2:
        raise ValueError("Mock embedding dimension must be at least 2")
    result = [0.0] * dimension
    result[0] = 0.01
    terms = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.casefold())
    for token in terms:
        hashed = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        result[1 + hashed % (dimension - 1)] += 1.0
    norm = math.sqrt(sum(value * value for value in result))
    return [value / norm for value in result]
