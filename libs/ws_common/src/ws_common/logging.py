"""Структурные логи сервисов: structlog (рабочая зависимость) с резервом на stdlib logging.

Формат события — `ts, level, service, event, correlation_id, job_id, rpc, duration_ms` (§9 ТЗ);
значения ключей, похожих на секреты, маскируются (§14.4). Резервный логгер сохраняет состав полей,
чтобы код прикладного слоя не зависел от наличия structlog в конкретном окружении.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Protocol

_SECRET_KEY_RE = re.compile(r"(api_key|apikey|token|password|credentials|authorization|secret)", re.IGNORECASE)
_MASK = "***"

try:
    import structlog

    STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover - ветка выбирается составом окружения
    STRUCTLOG_AVAILABLE = False


class Logger(Protocol):
    """Минимальный интерфейс структурного логгера, используемый прикладным слоем."""

    def debug(self, event: str, **fields: Any) -> None: ...

    def info(self, event: str, **fields: Any) -> None: ...

    def warning(self, event: str, **fields: Any) -> None: ...

    def error(self, event: str, **fields: Any) -> None: ...


def mask_secrets(_logger: object, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Маскирует значения ключей, похожих на секреты, включая вложенные словари."""
    for key, value in list(event_dict.items()):
        if _SECRET_KEY_RE.search(key):
            event_dict[key] = _MASK
        elif isinstance(value, dict):
            event_dict[key] = mask_secrets(_logger, _name, dict(value))
    return event_dict


class _StdlibLogger:
    """Резервная реализация: событие и поля выводятся одной JSON-строкой через logging."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, fields: dict[str, Any]) -> None:
        payload = mask_secrets(None, "", {"event": event, **fields, **_CONTEXT})
        self._logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)


_CONTEXT: dict[str, Any] = {}


def configure_logging(service: str, level: str = "INFO", fmt: str = "json") -> None:
    """Настраивает вывод логов в stdout для выбранного формата."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level))
    _CONTEXT["service"] = service
    if not STRUCTLOG_AVAILABLE:  # pragma: no cover - см. комментарий к импорту
        return
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            mask_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str) -> Logger:
    """Логгер модуля: structlog при наличии, иначе stdlib."""
    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)  # type: ignore[no-any-return]
    return _StdlibLogger(name)
