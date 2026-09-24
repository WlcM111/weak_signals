"""Идентификаторы: UUID v7 (упорядочен по времени) и correlation id.

Первичные ключи генерирует PostgreSQL (`DEFAULT uuidv7()`); эта реализация нужна для
идентификаторов процессов, correlation id и тестовых данных.
"""

from __future__ import annotations

import os
import time
import uuid
from contextvars import ContextVar

# Идентификатор корреляции текущей операции (HTTP-запроса или задания): его читают исходящие
# gRPC-клиенты, чтобы логи всех сервисов по одному заданию находились одним поиском.
_CORRELATION_ID: ContextVar[str] = ContextVar("ws_correlation_id", default="")


def set_correlation_id(value: str) -> None:
    """Запоминает идентификатор корреляции для текущей задачи asyncio / потока."""
    _CORRELATION_ID.set((value or "")[:64])


def current_correlation_id() -> str:
    """Идентификатор корреляции текущей операции; если не задан — новый случайный."""
    return _CORRELATION_ID.get() or new_correlation_id()


def uuid7(timestamp_ms: int | None = None, random_bytes: bytes | None = None) -> uuid.UUID:
    """UUID версии 7 (RFC 9562): 48 бит времени в миллисекундах + 74 бита случайных данных."""
    ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp_ms вне диапазона 48 бит")
    rnd = os.urandom(10) if random_bytes is None else random_bytes
    if len(rnd) != 10:
        raise ValueError("random_bytes должен содержать 10 байт")
    value = bytearray(ms.to_bytes(6, "big") + rnd)
    value[6] = (value[6] & 0x0F) | 0x70  # версия 7
    value[8] = (value[8] & 0x3F) | 0x80  # вариант RFC 4122
    return uuid.UUID(bytes=bytes(value))


def new_correlation_id() -> str:
    """Correlation id для метаданных `x-correlation-id` (≤ 64 символов)."""
    return str(uuid.uuid4())


def worker_id(service_name: str) -> str:
    """Владелец аренды: имя сервиса + pid + короткий суффикс (уникален между репликами)."""
    return f"{service_name}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
