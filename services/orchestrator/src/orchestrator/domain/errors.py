"""Ошибки домена и приложения orchestrator (§14 HANDOFF: коды HTTP-ответов)."""

from __future__ import annotations

from ws_common.errors import (
    AppError,
    InternalError,
    NotFoundError,
    PreconditionFailedError,
    ResourceExhaustedError,
    UnavailableError,
    ValidationError,
)

__all__ = [
    "AppError",
    "IdempotencyConflict",
    "InternalError",
    "InvariantViolation",
    "JobNotCancellable",
    "LeaseLost",
    "NotFoundError",
    "PreconditionFailedError",
    "QueueFull",
    "RateLimited",
    "ResourceExhaustedError",
    "StageFailed",
    "Unauthorized",
    "UnavailableError",
    "UpstreamUnavailable",
    "ValidationError",
]


class InvariantViolation(AppError):
    """Нарушен инвариант доменной сущности."""

    error_code = "INVARIANT_VIOLATION"


class IdempotencyConflict(AppError):
    """Ключ идемпотентности уже использован с другим телом запроса (HTTP 409)."""

    error_code = "IDEMPOTENCY_CONFLICT"


class JobNotCancellable(AppError):
    """Задание уже завершено и не может быть отменено (HTTP 409)."""

    error_code = "JOB_NOT_CANCELLABLE"


class QueueFull(ResourceExhaustedError):
    """Очередь заданий заполнена (HTTP 429, Retry-After: 30)."""

    error_code = "QUEUE_FULL"


class RateLimited(ResourceExhaustedError):
    """Превышен лимит запросов клиента (HTTP 429)."""

    error_code = "RATE_LIMITED"


class Unauthorized(AppError):
    """Отсутствует или неверен API-ключ (HTTP 401)."""

    error_code = "UNAUTHORIZED"


class UpstreamUnavailable(UnavailableError):
    """Вызываемый сервис недоступен (HTTP 502)."""

    error_code = "UPSTREAM_UNAVAILABLE"


class LeaseLost(AppError):
    """Аренда задания потеряна: его выполняет другой воркер."""

    error_code = "LEASE_EXPIRED"


class StageFailed(AppError):
    """Стадия задания завершилась отказом; несёт код для `jobs.error_code`."""

    def __init__(self, error_code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
