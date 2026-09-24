"""Сквозной срок одного задания: от приёма запроса до сохранённого результата.

Срок отсчитывается от `created_at` — момента, когда API принял задание и записал его в БД. Поэтому
он включает ожидание в очереди и переживает перезапуск воркера: после рестарта из того же
`created_at` получается тот же `deadline_at`. Резерв оставляет время на запись результата и
завершение задания; работа стадий планируется только в пределах срока за вычетом резерва.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class JobDeadline:
    """Абсолютный срок задания и резерв на завершение."""

    accepted_at: datetime
    deadline_at: datetime
    reserve_seconds: float

    @classmethod
    def for_job(cls, accepted_at: datetime, deadline_seconds: float, reserve_seconds: float) -> JobDeadline:
        """Срок задания, принятого в `accepted_at`."""
        return cls(accepted_at, accepted_at + timedelta(seconds=deadline_seconds), reserve_seconds)

    def remaining(self, now: datetime) -> float:
        """Секунд до срока."""
        return (self.deadline_at - now).total_seconds()

    def usable(self, now: datetime) -> float:
        """Секунд до срока за вычетом резерва на завершение."""
        return self.remaining(now) - self.reserve_seconds

    def expired(self, now: datetime) -> bool:
        """Работы по заданию начинать нельзя: не осталось времени сверх резерва."""
        return self.usable(now) <= 0

    def allows(self, now: datetime, seconds: float) -> bool:
        """Успеет ли операция длительностью до `seconds` завершиться до резерва."""
        return self.usable(now) >= seconds

    def cap(self, now: datetime, configured: float, floor: float, keep_for_later: float) -> int:
        """Лимит стадии: не больше настроенного и не больше времени, оставшегося после следующих стадий."""
        return int(max(floor, min(configured, self.usable(now) - keep_for_later)))
