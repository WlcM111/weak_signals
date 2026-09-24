"""Token bucket на адаптер источника (§13.4 ТЗ).

Ёмкость = `rate_limit_rps × 5`, пополнение = `rate_limit_rps`; лимит делится на число реплик.
Ответ 429 → пауза `Retry-After` и снижение лимита вдвое на 5 минут.
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
CAPACITY_MULTIPLIER = 5.0


@dataclass(slots=True)
class _Bucket:
    """Состояние одного ведра токенов."""

    base_rate: float
    rate: float
    capacity: float
    tokens: float
    updated_at: float
    penalty_until: float = 0.0


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
                capacity=max(1.0, rate / replicas * CAPACITY_MULTIPLIER),
                tokens=max(1.0, rate / replicas * CAPACITY_MULTIPLIER),
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
            if bucket.tokens < 1.0:
                wait = (1.0 - bucket.tokens) / bucket.rate
                if self._on_wait is not None:
                    self._on_wait(source_key)
                await self._clock.sleep(wait)
                self._refill(bucket)
            bucket.tokens = max(0.0, bucket.tokens - 1.0)

    async def penalize(self, source_key: SourceKey, retry_after: float | None) -> None:
        """Пауза после 429 и временное снижение лимита источника."""
        bucket = self._buckets.get(source_key)
        pause = DEFAULT_RETRY_AFTER if retry_after is None else max(0.0, retry_after)
        if bucket is not None:
            async with self._locks[source_key]:
                bucket.rate = max(bucket.base_rate * PENALTY_FACTOR, 0.01)
                bucket.penalty_until = self._clock.monotonic() + PENALTY_SECONDS
                bucket.tokens = 0.0
                bucket.updated_at = self._clock.monotonic()
        await self._clock.sleep(pause)

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
