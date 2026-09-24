"""Token bucket адаптеров источников (§13.4 ТЗ) на виртуальном времени."""

from __future__ import annotations

import unittest

from collector.adapters.outbound.rate_limiter import (
    CAPACITY_MULTIPLIER,
    PENALTY_SECONDS,
    TokenBucketRateLimiter,
)
from collector.domain.values import SourceKey

from ..fakes import FakeClock


class RateLimiterTest(unittest.IsolatedAsyncioTestCase):
    """Ёмкость, пополнение, штраф за 429 и деление лимита между репликами."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.waits: list[SourceKey] = []
        self.limiter = TokenBucketRateLimiter(
            {SourceKey.ARXIV: 1.0, SourceKey.OPENALEX: 5.0},
            self.clock,
            on_wait=self.waits.append,
        )

    async def test_initial_burst_is_capacity(self) -> None:
        for _ in range(int(1.0 * CAPACITY_MULTIPLIER)):
            await self.limiter.acquire(SourceKey.ARXIV)
        self.assertEqual(self.clock.slept, [])

    async def test_waits_when_bucket_is_empty(self) -> None:
        for _ in range(int(1.0 * CAPACITY_MULTIPLIER) + 1):
            await self.limiter.acquire(SourceKey.ARXIV)
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], 1.0, places=3)
        self.assertEqual(self.waits, [SourceKey.ARXIV])

    async def test_refill_over_time(self) -> None:
        for _ in range(5):
            await self.limiter.acquire(SourceKey.ARXIV)
        self.clock.advance(3.0)
        for _ in range(3):
            await self.limiter.acquire(SourceKey.ARXIV)
        self.assertEqual(self.clock.slept, [])

    async def test_penalty_halves_rate_and_sleeps_retry_after(self) -> None:
        await self.limiter.penalize(SourceKey.OPENALEX, retry_after=7.0)
        self.assertEqual(self.clock.slept, [7.0])
        self.assertAlmostEqual(self.limiter.rate_of(SourceKey.OPENALEX), 2.5)

    async def test_penalty_expires(self) -> None:
        await self.limiter.penalize(SourceKey.OPENALEX, retry_after=0.0)
        self.clock.advance(PENALTY_SECONDS + 1)
        await self.limiter.acquire(SourceKey.OPENALEX)
        self.assertAlmostEqual(self.limiter.rate_of(SourceKey.OPENALEX), 5.0)

    async def test_default_pause_without_retry_after(self) -> None:
        await self.limiter.penalize(SourceKey.ARXIV, retry_after=None)
        self.assertEqual(self.clock.slept, [5.0])

    async def test_replicas_divide_rate(self) -> None:
        limiter = TokenBucketRateLimiter({SourceKey.OPENALEX: 5.0}, FakeClock(), replicas=5)
        self.assertAlmostEqual(limiter.rate_of(SourceKey.OPENALEX), 1.0)

    async def test_unknown_source_is_not_limited(self) -> None:
        await self.limiter.acquire(SourceKey.HH)
        self.assertEqual(self.clock.slept, [])

    def test_replicas_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter({}, FakeClock(), replicas=0)


if __name__ == "__main__":
    unittest.main()
