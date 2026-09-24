"""Ошибки домена и провайдеров LLM (§14 HANDOFF)."""

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
    "BudgetExhausted",
    "InternalError",
    "InvariantViolation",
    "NotFoundError",
    "PreconditionFailedError",
    "ProviderBlocked",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderTimeout",
    "ProvidersUnavailable",
    "ResourceExhaustedError",
    "UnavailableError",
    "ValidationError",
]


class InvariantViolation(AppError):
    """Нарушен инвариант доменной сущности."""

    error_code = "INVARIANT_VIOLATION"


class ProviderError(AppError):
    """Провайдер LLM ответил ошибкой. Сообщение никогда не содержит текста промпта (§14)."""

    error_code = "PROVIDER_ERROR"
    call_status = "PROVIDER_ERROR"

    def __init__(self, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider


class ProviderRateLimited(ProviderError):
    """Провайдер вернул 429; в `retry_after_seconds` — рекомендованная пауза."""

    error_code = "PROVIDER_RATE_LIMITED"
    call_status = "RATE_LIMITED"

    def __init__(self, message: str, provider: str = "", retry_after_seconds: float = 5.0) -> None:
        super().__init__(message, provider)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeout(ProviderError):
    """Провайдер не ответил за отведённый дедлайн."""

    error_code = "PROVIDER_TIMEOUT"
    call_status = "TIMEOUT"


class ProviderBlocked(ProviderError):
    """Запрос или ответ отклонён контент-фильтром провайдера."""

    error_code = "PROVIDER_BLOCKED"
    call_status = "BLOCKED"


class BudgetExhausted(ProviderError):
    """Исчерпан суточный бюджет токенов: вызовы LLM прекращаются до следующих суток."""

    error_code = "BUDGET_EXHAUSTED"
    call_status = "PROVIDER_ERROR"


class ProvidersUnavailable(UnavailableError):
    """Ни один провайдер не смог сформировать ответ, а fallback запрещён клиентом."""

    error_code = "PROVIDERS_UNAVAILABLE"
