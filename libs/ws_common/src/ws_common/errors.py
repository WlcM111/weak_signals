"""Базовые ошибки приложения, общие для всех сервисов.

Слой application/domain бросает эти ошибки; транспортные адаптеры переводят их в коды gRPC/HTTP.
"""

from __future__ import annotations


class AppError(Exception):
    """Ошибка с машинным кодом (`error_code`) и человекочитаемым сообщением на русском."""

    error_code: str = "INTERNAL"

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if error_code is not None:
            self.error_code = error_code


class ValidationError(AppError):
    """Нарушены правила валидации запроса (§10.1 ТЗ). → gRPC INVALID_ARGUMENT."""

    error_code = "INVALID_ARGUMENT"


class NotFoundError(AppError):
    """Запрошенная сущность не существует. → gRPC NOT_FOUND."""

    error_code = "NOT_FOUND"


class PreconditionFailedError(AppError):
    """Состояние не позволяет выполнить операцию. → gRPC FAILED_PRECONDITION."""

    error_code = "FAILED_PRECONDITION"


class ResourceExhaustedError(AppError):
    """Превышен лимит очереди/ресурса. → gRPC RESOURCE_EXHAUSTED."""

    error_code = "RESOURCE_EXHAUSTED"


class UnavailableError(AppError):
    """Внешняя зависимость (БД) недоступна. → gRPC UNAVAILABLE."""

    error_code = "UNAVAILABLE"


class InternalError(AppError):
    """Непредвиденная ошибка сервера. → gRPC INTERNAL."""

    error_code = "INTERNAL"
