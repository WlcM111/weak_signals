"""Ошибки домена и приложения analyzer (коды — §14 HANDOFF, §10.1 ТЗ)."""

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
    "CollectorUnavailable",
    "InternalError",
    "InvariantViolation",
    "LeaseLost",
    "ModelNotLoaded",
    "NotFoundError",
    "PreconditionFailedError",
    "ResourceExhaustedError",
    "UnavailableError",
    "ValidationError",
]


class InvariantViolation(AppError):
    """Нарушен инвариант доменной сущности (ошибка программирования, не входных данных)."""

    error_code = "INVARIANT_VIOLATION"


class LeaseLost(AppError):
    """Аренда анализа потеряна (перехвачена другим воркером или истекла)."""

    error_code = "LEASE_EXPIRED"


class ModelNotLoaded(PreconditionFailedError):
    """Активная модель не загружена: сервис не может скорить."""

    error_code = "MODEL_NOT_LOADED"


class CollectorUnavailable(UnavailableError):
    """Сервис collector недоступен или ответил ошибкой."""

    error_code = "COLLECTOR_UNAVAILABLE"
