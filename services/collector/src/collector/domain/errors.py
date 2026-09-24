"""Доменные и прикладные ошибки collector (коды — §10.1, §10.7 ТЗ)."""

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
    "AdapterFailure",
    "AppError",
    "InternalError",
    "InvariantViolation",
    "LeaseLost",
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
    """Аренда коллекции потеряна (перехвачена другим воркером или истекла) — работу нужно прекратить."""

    error_code = "LEASE_EXPIRED"


class AdapterFailure(Exception):
    """Отказ адаптера источника с кодом из единого списка (§10.7). Наружу не выходит: пишется в adapter_runs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
