"""Зависимости HTTP: API-ключ, correlation id, ограничение частоты запросов (§17 HANDOFF)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from orchestrator.application.validation import check_api_key
from orchestrator.domain.errors import RateLimited, Unauthorized

MAX_BODY_BYTES = 64 * 1024


@dataclass(slots=True)
class ApiKeyGuard:
    """Проверка заголовка `X-API-Key` сравнением за постоянное время."""

    expected: str = ""

    def check(self, provided: str | None) -> None:
        """Пропускает запрос либо поднимает 401."""
        if not check_api_key(provided, self.expected):
            raise Unauthorized("требуется корректный заголовок X-API-Key")

    @property
    def enabled(self) -> bool:
        """Включена ли аутентификация."""
        return bool(self.expected)


@dataclass(slots=True)
class RateLimiter:
    """Скользящее окно в одну минуту по хешу клиента; состояние в памяти процесса."""

    limit_per_minute: int = 30
    window_seconds: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def check(self, client_key: str | None, now: float) -> None:
        """Регистрирует обращение; при превышении лимита поднимает 429."""
        if self.limit_per_minute <= 0 or not client_key:
            return
        window = self._hits.setdefault(client_key, deque())
        while window and now - window[0] > self.window_seconds:
            window.popleft()
        if len(window) >= self.limit_per_minute:
            raise RateLimited(
                f"превышен лимит {self.limit_per_minute} запросов в минуту, повторите позже"
            )
        window.append(now)

    def reset(self) -> None:
        """Сбрасывает состояние (используется в тестах)."""
        self._hits.clear()


def check_body_size(size: int) -> None:
    """Лимит тела запроса 64 КБ (§17 HANDOFF)."""
    if size > MAX_BODY_BYTES:
        from orchestrator.domain.errors import ValidationError  # noqa: PLC0415 - избегаем цикла

        raise ValidationError("тело запроса больше 64 КБ", "VALIDATION_ERROR")
