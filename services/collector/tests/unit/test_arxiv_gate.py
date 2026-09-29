"""Шлюз адаптера arXiv без сети: интервал, общая очередь и пауза после отказа."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from collector.adapters.outbound.sources import arxiv
from collector.adapters.outbound.sources.arxiv import ArxivAdapter, reset_arxiv_gate
from collector.application.ports import HttpTimeoutError, HttpTransportError
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

TERMS = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic chips",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
DEADLINE = 10_000.0
EMPTY_FEED = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


class ArxivGateTest(unittest.IsolatedAsyncioTestCase):
    """Общий шлюз процесса переживает экземпляры адаптера, поэтому каждый тест начинает с его сброса."""

    def setUp(self) -> None:
        reset_arxiv_gate()
        self.http = FakeHttpClient()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        reset_arxiv_gate()

    async def collect(self) -> list[Any]:
        """Всё, что выдал новый экземпляр адаптера."""
        adapter = ArxivAdapter(self.http, FakeRateLimiter(), self.clock)
        return [document async for document in adapter.search(TERMS, LIMITS, DEADLINE)]

    async def assert_cooldown_after(self, failure: Exception, expected_code: str) -> AdapterFailure:
        """Отказ включает паузу: следующий сбор не идёт в сеть и сообщает тот же код."""
        self.http.enqueue(failure)
        with self.assertRaises(AdapterFailure) as first:
            await self.collect()
        self.assertEqual(first.exception.code, expected_code)
        with self.assertRaises(AdapterFailure) as second:
            await self.collect()
        self.assertEqual(second.exception.code, expected_code)
        self.assertIn("пауза", second.exception.message)
        self.assertEqual(len(self.http.calls), 1)
        return first.exception

    async def test_406_reported_as_unknown_4xx_and_pauses(self) -> None:
        # Причина 406 не установлена: код HTTP_4XX, а не RATE_LIMITED; пауза источника сохраняется.
        failure = await self.assert_cooldown_after(status_error(406), AdapterErrorCode.HTTP_4XX.value)
        self.assertIn("406", failure.message)
        self.assertIn("причина не установлена", failure.message)

    async def test_cooldown_after_429(self) -> None:
        await self.assert_cooldown_after(status_error(429), AdapterErrorCode.RATE_LIMITED.value)

    async def test_cooldown_after_403(self) -> None:
        await self.assert_cooldown_after(status_error(403), AdapterErrorCode.HTTP_4XX.value)

    async def test_cooldown_after_server_error(self) -> None:
        await self.assert_cooldown_after(status_error(503), AdapterErrorCode.HTTP_5XX.value)

    async def test_no_cooldown_after_timeout(self) -> None:
        # медленный ответ — не отказ сервера: следующий запрос разрешён (стенд 29.09: пауза после таймаута
        # выключала arXiv на 30 минут для следующих тем)
        self.http.enqueue(HttpTimeoutError("таймаут"))
        self.http.enqueue(EMPTY_FEED)
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.TIMEOUT.value)
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)

    async def test_cooldown_after_network_error(self) -> None:
        await self.assert_cooldown_after(HttpTransportError("разрыв"), AdapterErrorCode.HTTP_5XX.value)

    async def test_no_cooldown_after_bad_request(self) -> None:
        self.http.enqueue(status_error(400))
        self.http.enqueue(EMPTY_FEED)
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.HTTP_4XX.value)
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)

    async def test_cooldown_expires(self) -> None:
        self.http.enqueue(status_error(406))
        self.http.enqueue(EMPTY_FEED)
        with self.assertRaises(AdapterFailure):
            await self.collect()
        self.clock.advance(arxiv.COOLDOWN_SECONDS + 1)
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)

    async def test_interval_between_requests(self) -> None:
        self.http.enqueue(EMPTY_FEED)
        self.http.enqueue(EMPTY_FEED)
        await self.collect()
        await self.collect()
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], arxiv.MIN_GAP_SECONDS, places=6)

    async def test_parallel_collections_share_one_queue(self) -> None:
        self.http.enqueue(EMPTY_FEED)
        self.http.enqueue(EMPTY_FEED)
        await asyncio.gather(self.collect(), self.collect())
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], arxiv.MIN_GAP_SECONDS, places=6)

    async def test_request_headers(self) -> None:
        self.http.enqueue(EMPTY_FEED)
        await self.collect()
        self.assertEqual(self.http.calls[0][1]["headers"], {"Accept": "application/atom+xml, application/xml;q=0.9"})


if __name__ == "__main__":
    unittest.main()