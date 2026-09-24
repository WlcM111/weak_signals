"""Учёт HTTP-запросов адаптеров в пределах одного выполнения сбора.

Экземпляры адаптеров создаются один раз на процесс и обслуживают все коллекции, поэтому их
собственные счётчики накапливаются между заданиями. Учёт одного сбора хранится в контекстной
переменной: сценарий сбора открывает его перед запуском адаптеров, а задачи адаптеров, созданные
внутри сценария (`asyncio.TaskGroup`), наследуют контекст и пишут в тот же объект. Параллельные
сборы выполняются в разных задачах воркеров и получают разные объекты учёта, поэтому не
засчитывают запросы друг друга.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from collector.domain.values import SourceKey


@dataclass
class RequestAccounting:
    """Счётчики одного сбора: HTTP-запросы и серии ответов 429 подряд по источникам."""

    requests: dict[SourceKey, int] = field(default_factory=dict)
    consecutive_rate_limits: dict[SourceKey, int] = field(default_factory=dict)

    def count_request(self, source_key: SourceKey) -> None:
        """Засчитывает один HTTP-запрос источника."""
        self.requests[source_key] = self.requests.get(source_key, 0) + 1

    def requests_of(self, source_key: SourceKey) -> int:
        """Число HTTP-запросов источника в этом сборе."""
        return self.requests.get(source_key, 0)

    def bump_rate_limit(self, source_key: SourceKey) -> int:
        """Увеличивает серию ответов 429 подряд и возвращает её длину."""
        streak = self.consecutive_rate_limits.get(source_key, 0) + 1
        self.consecutive_rate_limits[source_key] = streak
        return streak

    def reset_rate_limit(self, source_key: SourceKey) -> None:
        """Обрывает серию ответов 429 после успешного ответа."""
        self.consecutive_rate_limits[source_key] = 0


_CURRENT: ContextVar[RequestAccounting | None] = ContextVar("collector_request_accounting", default=None)


def begin_request_accounting() -> RequestAccounting:
    """Открывает учёт для текущего контекста и задач, которые будут созданы из него."""
    accounting = RequestAccounting()
    _CURRENT.set(accounting)
    return accounting


def current_request_accounting() -> RequestAccounting | None:
    """Учёт текущего сбора; None вне сбора (например, при проверке энциклопедии)."""
    return _CURRENT.get()