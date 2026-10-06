"""Адаптер Semantic Scholar: bulk-поиск (этап 3, B3) и политика пауз после 429 (B2-b) на записанных ответах."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from collector.adapters.outbound.sources.semantic_scholar import (
    API_URL,
    SemanticScholarAdapter,
    bulk_query,
    window_start,
)
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import DEFAULT_POLICIES, SourcePolicy
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "semantic_scholar" / "search_success.json"
TERMS = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic chips",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
DEADLINE = 10_000.0


def page(papers: list, token: str | None = None) -> dict:
    payload: dict = {"total": len(papers), "data": papers}
    if token:
        payload["token"] = token
    return payload


def papers(count: int, prefix: str = "p") -> list:
    return [{"paperId": f"{prefix}{n}", "title": f"Paper {prefix}{n}", "abstract": "a", "url": f"https://s2/{prefix}{n}",
             "publicationDate": "2026-05-01"} for n in range(count)]


class SemanticScholarTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()

    def adapter(self, api_key: str = "", policy: SourcePolicy | None = None) -> SemanticScholarAdapter:
        adapter = SemanticScholarAdapter(self.http, self.limiter, FakeClock(NOW), api_key, window_months=24)
        adapter.policy = policy or DEFAULT_POLICIES[SourceKey.SEMANTIC_SCHOLAR]
        return adapter

    async def collect(self, adapter: SemanticScholarAdapter, terms: SearchTerms = TERMS) -> list:  # type: ignore[type-arg]
        return [document async for document in adapter.search(terms, LIMITS, DEADLINE)]

    async def test_parses_documents_and_skips_empty(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]
        self.http.enqueue(page(data))
        documents = await self.collect(self.adapter())
        valid = [p for p in data if isinstance(p.get("title"), str) and p["title"].strip()]
        self.assertEqual(len(documents), len([p for p in valid if p.get("url") or p.get("paperId")]))
        self.assertTrue(all(d.matched_term == "neuromorphic chips" for d in documents))

    async def test_bulk_request_sorted_by_date_in_window_with_exact_phrase(self) -> None:
        self.http.enqueue(page(papers(3)))
        await self.collect(self.adapter())
        url, call = self.http.calls[0]
        self.assertEqual(url, API_URL)
        self.assertTrue(url.endswith("/paper/search/bulk"))
        params = call["params"]
        self.assertEqual(params["query"], '"neuromorphic chips"')
        self.assertEqual(params["sort"], "publicationDate:desc")
        self.assertEqual(params["publicationDateOrYear"], f"{window_start(NOW, 24, 2023).isoformat()}:")
        self.assertNotIn("offset", params)
        self.assertNotIn("token", params)

    async def test_token_continues_until_quota_and_stops_without_token(self) -> None:
        terms = SearchTerms(ru=(), en=("edge ai",))
        self.http.enqueue(page(papers(100, "a"), token="t1"))
        self.http.enqueue(page(papers(100, "b")))
        documents = await self.collect(self.adapter(), terms)
        self.assertEqual(len(documents), LIMITS.max_documents_per_source)
        self.assertEqual(self.http.calls[1][1]["params"]["token"], "t1")
        self.assertEqual(len(self.http.calls), 2)

    async def test_quota_is_shared_between_phrases(self) -> None:
        terms = SearchTerms(ru=(), en=("edge ai", "tiny ml", "npu inference"))
        for prefix in "xyz":
            self.http.enqueue(page(papers(80, prefix)))
        documents = await self.collect(self.adapter(), terms)
        self.assertEqual(len(documents), LIMITS.max_documents_per_source)
        self.assertEqual([c[1]["params"]["query"] for c in self.http.calls], ['"edge ai"', '"tiny ml"', '"npu inference"'])
        self.assertEqual(sum(1 for d in documents if d.matched_term == "edge ai"), 50)

    async def test_rate_limit_pause_then_retry_keeps_results(self) -> None:
        self.http.enqueue(status_error(429))
        self.http.enqueue(page(papers(5)))
        documents = await self.collect(self.adapter())
        self.assertEqual(len(documents), 5)
        self.assertEqual(self.limiter.penalties, [(SourceKey.SEMANTIC_SCHOLAR, 10.0)])  # пауза политики без Retry-After

    async def test_retry_after_from_server_wins(self) -> None:
        self.http.enqueue(status_error(429, retry_after=3.0))
        self.http.enqueue(page(papers(2)))
        await self.collect(self.adapter())
        self.assertEqual(self.limiter.penalties, [(SourceKey.SEMANTIC_SCHOLAR, 3.0)])

    async def test_rate_limit_beyond_retries_fails(self) -> None:
        for _ in range(3):
            self.http.enqueue(status_error(429))
        with self.assertRaises(AdapterFailure) as raised:
            await self.collect(self.adapter())
        self.assertEqual(raised.exception.code, AdapterErrorCode.RATE_LIMITED.value)
        self.assertEqual(len(self.http.calls), 3)  # запрос и два повтора

    async def test_request_budget_is_enforced(self) -> None:
        terms = SearchTerms(ru=(), en=("edge ai",))
        self.http.enqueue(page(papers(10, "a"), token="t1"))
        documents: list = []
        with self.assertRaises(AdapterFailure) as raised:
            async for document in self.adapter(policy=SourcePolicy(max_requests=1)).search(terms, LIMITS, DEADLINE):
                documents.append(document)
        self.assertEqual(raised.exception.code, AdapterErrorCode.BUDGET_EXHAUSTED.value)
        self.assertEqual(len(documents), 10)  # документы первой страницы сохранены
        self.assertEqual(len(self.http.calls), 1)

    async def test_api_key_sent_as_header(self) -> None:
        self.http.enqueue(page([]))
        await self.collect(self.adapter(api_key="secret"))
        self.assertEqual(self.http.calls[0][1]["headers"], {"x-api-key": "secret"})

    async def test_no_key_no_header(self) -> None:
        self.http.enqueue(page([]))
        await self.collect(self.adapter())
        self.assertEqual(self.http.calls[0][1]["headers"], {})

    async def test_malformed_payload_is_parse_error(self) -> None:
        self.http.enqueue({"data": "не список"})
        with self.assertRaises(AdapterFailure) as raised:
            await self.collect(self.adapter())
        self.assertEqual(raised.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    def test_bulk_query_and_window(self) -> None:
        self.assertEqual(bulk_query('AI-based "robot" (control)'), '"AI based robot control"')
        self.assertEqual(bulk_query("robotics"), "robotics")
        self.assertEqual(bulk_query(' - + '), "")
        self.assertEqual(window_start(NOW, 24, 2023), date(2024, 10, 3))  # 24 × 30,44 ≈ 731 день
        self.assertEqual(window_start(NOW, 24, 2026), date(2026, 1, 1))
        self.assertEqual(SemanticScholarAdapter.key, SourceKey.SEMANTIC_SCHOLAR)


if __name__ == "__main__":
    unittest.main()
