"""Token bucket на адаптер источника (§13.4 ТЗ).

Этап 4.2 (аудит API 07.10.2026, п. 2.2–2.3):
- ёмкость 1 — без стартового всплеска: соседние запросы источника не чаще `1 / rate_limit_rps`
  (прежняя ёмкость `rate × 5` разрешала первые запросы почти одновременно);
- общая пауза `blocked_until`: после 429 или квотного 403 ни одна задача источника не получает разрешение раньше
  срока `Retry-After` (прежде засыпала только задача, получившая отказ); новая пауза только продлевает старую;
- 429 дополнительно снижает лимит вдвое на 5 минут; `defer` — короткая общая пауза без снижения лимита
  (повтор после сетевого сбоя или 5xx).
Лимит делится на число реплик; общий счётчик между процессами не ведётся — стенд работает одним процессом.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from collector.domain.values import SourceKey
from ws_common.clock import Clock

PENALTY_SECONDS = 300.0
PENALTY_FACTOR = 0.5
DEFAULT_RETRY_AFTER = 5.0


@dataclass(slots=True)
class _Bucket:
    """Состояние одного ведра токенов."""

    base_rate: float
    rate: float
    capacity: float
    tokens: float
    updated_at: float
    penalty_until: float = 0.0
    blocked_until: float = 0.0


class TokenBucketRateLimiter:
    """Ограничитель частоты запросов с общей блокировкой на каждый источник."""

    def __init__(
        self,
        rates: Mapping[SourceKey, float],
        clock: Clock,
        replicas: int = 1,
        on_wait: Callable[[SourceKey], None] | None = None,
    ) -> None:
        if replicas < 1:
            raise ValueError("replicas ≥ 1")
        now = clock.monotonic()
        self._clock = clock
        self._on_wait = on_wait
        self._buckets = {
            source_key: _Bucket(
                base_rate=rate / replicas,
                rate=rate / replicas,
                capacity=1.0,
                tokens=1.0,
                updated_at=now,
            )
            for source_key, rate in rates.items()
            if rate > 0
        }
        self._locks = {source_key: asyncio.Lock() for source_key in self._buckets}

    async def acquire(self, source_key: SourceKey) -> None:
        """Ожидает один токен источника; неизвестный источник не ограничивается."""
        bucket = self._buckets.get(source_key)
        if bucket is None:
            return
        async with self._locks[source_key]:
            self._refill(bucket)
            while True:
                now = self._clock.monotonic()
                blocked = max(0.0, bucket.blocked_until - now)
                missing = 0.0 if bucket.tokens >= 1.0 else (1.0 - bucket.tokens) / bucket.rate
                wait = max(blocked, missing)
                if wait <= 0.0:
                    break
                seen = bucket.blocked_until
                if self._on_wait is not None:
                    self._on_wait(source_key)
                await self._clock.sleep(wait)
                self._refill(bucket)
                if bucket.blocked_until <= seen:
                    break  # за время ожидания новой общей паузы не появилось — разрешение выдано
            bucket.tokens = max(0.0, bucket.tokens - 1.0)

    async def penalize(self, source_key: SourceKey, retry_after: float | None) -> None:
        """429 или квотный 403: общая пауза всем задачам источника и снижение лимита вдвое на 5 минут.

        Вызывающий не засыпает сам: следующий `acquire` любой задачи дождётся конца паузы.
        """
        pause = DEFAULT_RETRY_AFTER if retry_after is None else max(0.0, retry_after)
        bucket = self._buckets.get(source_key)
        if bucket is None:
            await self._clock.sleep(pause)  # неограниченный источник: пауза только для вызывающего
            return
        now = self._clock.monotonic()
        bucket.blocked_until = max(bucket.blocked_until, now + pause)
        bucket.rate = max(bucket.base_rate * PENALTY_FACTOR, 0.01)
        bucket.penalty_until = now + PENALTY_SECONDS
        bucket.tokens = 0.0
        bucket.updated_at = now

    def defer(self, source_key: SourceKey, seconds: float) -> None:
        """Короткая общая пауза источника без снижения лимита: повтор после сетевого сбоя или 5xx."""
        bucket = self._buckets.get(source_key)
        if bucket is not None and seconds > 0:
            bucket.blocked_until = max(bucket.blocked_until, self._clock.monotonic() + seconds)

    def blocked_for(self, source_key: SourceKey) -> float:
        """Сколько секунд источник ещё на общей паузе (диагностика и тесты)."""
        bucket = self._buckets.get(source_key)
        return max(0.0, bucket.blocked_until - self._clock.monotonic()) if bucket else 0.0

    def _refill(self, bucket: _Bucket) -> None:
        """Пополняет ведро и снимает штраф по истечении срока."""
        now = self._clock.monotonic()
        if bucket.penalty_until and now >= bucket.penalty_until:
            bucket.rate = bucket.base_rate
            bucket.penalty_until = 0.0
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.rate)
        bucket.updated_at = now

    def rate_of(self, source_key: SourceKey) -> float:
        """Текущий лимит источника (для тестов и диагностики)."""
        bucket = self._buckets.get(source_key)
        return bucket.rate if bucket else 0.0
