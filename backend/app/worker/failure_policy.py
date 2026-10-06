"""Classify structured exceptions, never digits found in human-readable messages."""

from dataclasses import dataclass

from fastapi import HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError

from app.errors import ModelRoutingError, PlatformError, TracePersistenceError


@dataclass(frozen=True)
class FailureDecision:
    code: str
    retryable: bool


def classify_failure(exc: BaseException) -> FailureDecision:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                APIConnectionError,
                APITimeoutError,
                RedisConnectionError,
                RedisTimeoutError,
                OperationalError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return FailureDecision("TRANSIENT_ERROR", True)
        if isinstance(current, (APIStatusError, HTTPException)):
            status = current.status_code
            return FailureDecision(
                "TRANSIENT_ERROR" if status in {408, 429} or status >= 500 else "PERMANENT_ERROR",
                status in {408, 429} or status >= 500,
            )
        if isinstance(current, TracePersistenceError):
            return FailureDecision("TRANSIENT_ERROR", True)
        if isinstance(current, ModelRoutingError) and current.last_error is not None:
            current = current.last_error
            continue
        if isinstance(current, PlatformError):
            return FailureDecision(current.code, False)
        current = current.__cause__
    return FailureDecision("UNKNOWN_ERROR", False)
