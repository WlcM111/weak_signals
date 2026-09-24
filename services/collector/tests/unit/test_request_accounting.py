"""Учёт HTTP-запросов по сбору: адаптеры общие на процесс, но счётчики сбора не накапливаются."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from typing import Any

from collector.adapters.outbound.sources.base import MAX_CONSECUTIVE_RATE_LIMITS, BaseSourceAdapter
from collector.application.dto import CollectionDraft
from collector.application.request_accounting import begin_request_accounting, current_request_accounting
from collector.domain.entities import Collection, RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import (
    AdapterErrorCode,
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
)

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, raw_document, status_error
from .test_run_collection import OWNER, RunCollectionTestBase

TERMS = SearchTerms(ru=("защита ИИ",), en=("ai security",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)


class PagingAdapter(BaseSourceAdapter):
    """Настоящий адаптер на базовом классе: по одному HTTP-запросу на страницу."""

    key = SourceKey.OPENALEX
    allowed_hosts = frozenset({"example.org"})

    def __init__(self, http: FakeHttpClient, limiter: FakeRateLimiter, clock: FakeClock) -> None:
        super().__init__(http, limiter, clock)
        self.pages = 1

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        for page in range(self.pages):
            payload = await self.fetch_json("https://example.org/api", params={"page": page})
            for url in payload["urls"]:
                yield raw_document(url, f"Публикация {url}")


async def drain(adapter: BaseSourceAdapter) -> list[Any]:
    """Всё, что выдал адаптер вне сценария сбора."""
    return [document async for document in adapter.search(TERMS, LIMITS, 10_000.0)]


class SequentialCollectionsTest(RunCollectionTestBase):
    """Дефект из отчёта аналитики: счётчик запросов рос от задания к заданию."""

    async def new_collection(self, key: str) -> Collection:
        """Коллекция с собственным ключом идемпотентности, в статусе RUNNING."""
        collection, _ = await self.collections.create_if_absent(
            CollectionDraft(
                idempotency_key=key,
                query_text="технологии защиты ИИ-систем",
                mode=CollectionMode.SEARCH,
                terms=TERMS,
                limits=LIMITS,
                sources=(SourceKey.OPENALEX,),
            )
        )
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        return collection

    async def test_same_adapter_instance_counts_each_collection_separately(self) -> None:
        http = FakeHttpClient()
        adapter = PagingAdapter(http, FakeRateLimiter(), self.clock)
        use_case = self.build({SourceKey.OPENALEX: adapter})

        http.enqueue({"urls": ["https://example.org/a1"]})
        http.enqueue({"urls": ["https://example.org/a2"]})
        adapter.pages = 2
        first = await self.new_collection("job-0001:collect")
        await use_case.execute(first, OWNER)

        for index in range(3):
            http.enqueue({"urls": [f"https://example.org/b{index}"]})
        adapter.pages = 3
        second = await self.new_collection("job-0002:collect")
        await use_case.execute(second, OWNER)

        first_run = (await self.runs.list_runs(first.collection_id))[0]
        second_run = (await self.runs.list_runs(second.collection_id))[0]
        self.assertEqual(first_run.http_requests, 2)
        self.assertEqual(second_run.http_requests, 3, "до исправления здесь было 5: счётчик экземпляра")
        self.assertEqual(first.http_requests_total, 2)
        self.assertEqual(second.http_requests_total, 3)
        self.assertEqual(adapter.http_requests, 5, "счётчик экземпляра по-прежнему считает за всю жизнь")


class RateLimitStreakTest(unittest.IsolatedAsyncioTestCase):
    """Серия ответов 429 подряд ведётся в пределах сбора, а не жизни экземпляра."""

    def adapter(self, http: FakeHttpClient) -> PagingAdapter:
        return PagingAdapter(http, FakeRateLimiter(), FakeClock())

    async def failure_after_429(self, adapter: PagingAdapter, http: FakeHttpClient) -> AdapterFailure:
        http.enqueue(status_error(429))
        with self.assertRaises(AdapterFailure) as error:
            await drain(adapter)
        return error.exception

    async def test_streak_does_not_leak_into_next_collection(self) -> None:
        http = FakeHttpClient()
        adapter = self.adapter(http)
        begin_request_accounting()
        for _ in range(MAX_CONSECUTIVE_RATE_LIMITS - 1):
            await self.failure_after_429(adapter, http)
        begin_request_accounting()  # следующий сбор
        failure = await self.failure_after_429(adapter, http)
        self.assertEqual(failure.code, AdapterErrorCode.RATE_LIMITED.value)
        self.assertEqual(failure.message, "источник ответил 429")

    async def test_streak_within_one_collection(self) -> None:
        http = FakeHttpClient()
        adapter = self.adapter(http)
        begin_request_accounting()
        for _ in range(MAX_CONSECUTIVE_RATE_LIMITS - 1):
            await self.failure_after_429(adapter, http)
        failure = await self.failure_after_429(adapter, http)
        self.assertIn(f"{MAX_CONSECUTIVE_RATE_LIMITS} ответа 429 подряд", failure.message)

    async def test_success_resets_streak(self) -> None:
        http = FakeHttpClient()
        adapter = self.adapter(http)
        begin_request_accounting()
        for _ in range(MAX_CONSECUTIVE_RATE_LIMITS - 1):
            await self.failure_after_429(adapter, http)
        http.enqueue({"urls": []})
        await drain(adapter)
        failure = await self.failure_after_429(adapter, http)
        self.assertEqual(failure.message, "источник ответил 429")


class IsolationTest(unittest.IsolatedAsyncioTestCase):
    """Параллельные сборы в разных задачах не засчитывают запросы друг друга."""

    async def test_parallel_collections_have_separate_accounting(self) -> None:
        http = FakeHttpClient()
        adapter = PagingAdapter(http, FakeRateLimiter(), FakeClock())
        for _ in range(5):
            http.enqueue({"urls": []})

        async def collection(pages: int) -> int:
            accounting = begin_request_accounting()
            for _ in range(pages):
                adapter.pages = 1
                await drain(adapter)
                await asyncio.sleep(0)
            return accounting.requests_of(SourceKey.OPENALEX)

        first, second = await asyncio.gather(
            asyncio.create_task(collection(2)), asyncio.create_task(collection(3))
        )
        self.assertEqual((first, second), (2, 3))
        self.assertEqual(adapter.http_requests, 5)

    async def test_requests_outside_collection_are_not_accounted(self) -> None:
        http = FakeHttpClient()
        http.enqueue({"urls": []})
        adapter = PagingAdapter(http, FakeRateLimiter(), FakeClock())
        self.assertIsNone(current_request_accounting())
        await drain(adapter)
        self.assertIsNone(current_request_accounting())
        self.assertEqual(adapter.http_requests, 1)


if __name__ == "__main__":
    unittest.main()