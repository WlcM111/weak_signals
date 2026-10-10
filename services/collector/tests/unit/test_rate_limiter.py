"""Token bucket источников (этап 4.2): без стартового всплеска, общая пауза после 429 для всех задач."""

from __future__ import annotations

import asyncio
import unittest

from collector.adapters.outbound.rate_limiter import (
    DEFAULT_RETRY_AFTER,
    PENALTY_FACTOR,
    PENALTY_SECONDS,
    TokenBucketRateLimiter,
)
from collector.domain.values import SourceKey

from ..fakes import FakeClock

S2 = SourceKey.SEMANTIC_SCHOLAR


class RateLimiterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.waits: list[SourceKey] = []
        self.limiter = TokenBucketRateLimiter({S2: 0.5}, self.clock, on_wait=self.waits.append)

    async def test_no_initial_burst(self) -> None:
        await self.limiter.acquire(S2)
        self.assertEqual(self.clock.slept, [])
        await self.limiter.acquire(S2)  # второй сразу за первым — ждёт полный интервал 1 / 0,5 = 2 с
        self.assertEqual(self.clock.slept, [2.0])
        self.assertEqual(self.waits, [S2])

    async def test_interval_kept_after_idle(self) -> None:
        await self.limiter.acquire(S2)
        self.clock.advance(60)  # простой не копит запас на всплеск
        await self.limiter.acquire(S2)
        await self.limiter.acquire(S2)
        self.assertEqual(self.clock.slept, [2.0])

    async def test_penalize_blocks_every_task_until_retry_after(self) -> None:
        await self.limiter.penalize(S2, 120.0)
        self.assertEqual(self.clock.slept, [])  # вызывающий не спит сам
        self.assertEqual(self.limiter.blocked_for(S2), 120.0)
        await asyncio.gather(self.limiter.acquire(S2), self.limiter.acquire(S2))
        self.assertEqual(self.clock.slept[0], 120.0)  # и первая, и вторая задача ждут общую паузу
        self.assertGreaterEqual(len(self.clock.slept), 2)

    async def test_later_shorter_penalty_does_not_shorten_pause(self) -> None:
        await self.limiter.penalize(S2, 120.0)
        await self.limiter.penalize(S2, 5.0)
        self.assertEqual(self.limiter.blocked_for(S2), 120.0)

    async def test_penalty_halves_rate_until_expiry(self) -> None:
        await self.limiter.penalize(S2, None)
        self.assertEqual(self.limiter.blocked_for(S2), DEFAULT_RETRY_AFTER)
        self.assertEqual(self.limiter.rate_of(S2), 0.5 * PENALTY_FACTOR)
        self.clock.advance(PENALTY_SECONDS + 1)
        await self.limiter.acquire(S2)
        self.assertEqual(self.limiter.rate_of(S2), 0.5)

    async def test_defer_pauses_without_rate_change(self) -> None:
        self.limiter.defer(S2, 2.0)
        self.assertEqual(self.limiter.blocked_for(S2), 2.0)
        self.assertEqual(self.limiter.rate_of(S2), 0.5)

    async def test_new_penalty_during_wait_extends_it(self) -> None:
        await self.limiter.acquire(S2)

        async def penalize_soon() -> None:  # 429 соседней задачи приходит, пока первая ждёт интервал
            await self.limiter.penalize(S2, 30.0)

        await asyncio.gather(self.limiter.acquire(S2), penalize_soon())
        self.assertIn(30.0, self.clock.slept)

    async def test_replicas_divide_rate_and_unknown_source_is_free(self) -> None:
        limiter = TokenBucketRateLimiter({S2: 1.0}, self.clock, replicas=2)
        self.assertEqual(limiter.rate_of(S2), 0.5)
        await limiter.acquire(SourceKey.OPENALEX)
        self.assertEqual(self.clock.slept, [])

    def test_replicas_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter({S2: 1.0}, self.clock, replicas=0)


if __name__ == "__main__":
    unittest.main()
