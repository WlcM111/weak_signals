"""Адаптер Hacker News Search (Algolia), этап 3.1: окна истории, все слова фразы, разбор, отказы, бюджет."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from collector.adapters.outbound.sources.base import history_windows, records_per_request
from collector.adapters.outbound.sources.hackernews import (
    API_URL,
    ITEM_URL,
    HackerNewsAdapter,
    hn_query,
    published_at,
    story_text,
)
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import DEFAULT_POLICIES
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TERMS = SearchTerms(ru=("периферийный ИИ",), en=("edge AI inference", "tiny ml"))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
STORY = {"objectID": "41000001", "title": "Xeal wants to power edge data centers with EV chargers",
         "url": "https://techcrunch.com/2026/09/01/xeal-edge/", "points": 120, "num_comments": 45,
         "created_at": "2026-09-01T10:00:00Z", "created_at_i": 1788256800}
ASK = {"objectID": "41000002", "title": "Show HN: TinyML runtime for microcontrollers", "url": None,
       "story_text": "<p>We built a runtime &amp; compiler.</p>", "points": 30, "num_comments": 8,
       "created_at_i": 1780000000}


def page(*hits: dict) -> dict:
    return {"hits": list(hits), "nbHits": len(hits), "page": 0, "nbPages": 1, "hitsPerPage": len(hits)}


class HackerNewsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()

    def adapter(self) -> HackerNewsAdapter:
        adapter = HackerNewsAdapter(self.http, self.limiter, FakeClock(NOW), history_months=24)
        adapter.policy = DEFAULT_POLICIES[SourceKey.HACKERNEWS]
        return adapter

    async def collect(self) -> tuple[list, AdapterFailure | None]:  # type: ignore[type-arg]
        documents, failure = [], None
        try:
            async for document in self.adapter().search(TERMS, LIMITS, 10_000.0):
                documents.append(document)
        except AdapterFailure as error:
            failure = error
        return documents, failure

    async def test_parses_stories_and_ask_posts(self) -> None:
        self.http.enqueue(page(STORY, ASK, {"objectID": "x", "title": ""}))
        for _ in range(17):
            self.http.enqueue(page())
        documents, failure = await self.collect()
        self.assertIsNone(failure)
        story, ask = documents
        self.assertEqual(story.url, STORY["url"])
        self.assertEqual(story.title, STORY["title"])
        self.assertEqual(story.engagement_count, 120)
        self.assertEqual(story.published_at, datetime.fromtimestamp(1788256800, tz=UTC))
        self.assertEqual(story.raw_meta, {"hn_id": "41000001", "points": 120, "num_comments": 45, "domain": "techcrunch.com"})
        self.assertEqual(story.matched_term, "edge AI inference")
        self.assertEqual(ask.url, ITEM_URL.format("41000002"))  # пост без ссылки — на обсуждение
        self.assertEqual(ask.text, "Show HN: TinyML runtime for microcontrollers We built a runtime & compiler.")

    async def test_requests_by_windows_all_words_within_budget(self) -> None:
        for _ in range(18):
            self.http.enqueue(page())
        _, failure = await self.collect()
        self.assertIsNone(failure)
        self.assertEqual(len(self.http.calls), 18)  # 2 фразы × 9 окон за 24 месяца = бюджет
        url, call = self.http.calls[0]
        params = call["params"]
        self.assertEqual(url, API_URL)
        self.assertEqual(params["query"], "edge AI inference")
        self.assertEqual(params["tags"], "story")
        self.assertEqual(params["hitsPerPage"], records_per_request(150, 18, 5, 100))
        windows = history_windows(NOW, 24, LIMITS.published_since_year)
        start, end = windows[0]
        self.assertEqual(params["numericFilters"], f"created_at_i>{int(start.timestamp())},created_at_i<{int(end.timestamp())}")
        self.assertEqual([c[1]["params"]["query"] for c in self.http.calls[9:10]], ["tiny ml"])  # вторая фраза после окон первой

    async def test_failed_window_does_not_stop_others_and_is_reported(self) -> None:
        self.http.enqueue(status_error(503))
        self.http.enqueue(status_error(503))  # этап 4.2: один повтор после 5xx — через лимитер, с общей паузой
        self.http.enqueue(page(STORY))
        for _ in range(16):
            self.http.enqueue(page())
        documents, failure = await self.collect()
        self.assertEqual(len(documents), 1)
        self.assertEqual(failure.code, AdapterErrorCode.HTTP_5XX.value)  # type: ignore[union-attr]
        self.assertIn("1 из 18", failure.message)  # type: ignore[union-attr]
        self.assertEqual(self.limiter.deferred, [(SourceKey.HACKERNEWS, 2.0)])

    async def test_rate_limit_retries_once_then_stops(self) -> None:
        self.http.enqueue(status_error(429))
        self.http.enqueue(status_error(429))
        documents, failure = await self.collect()
        self.assertEqual(documents, [])
        self.assertEqual(failure.code, AdapterErrorCode.RATE_LIMITED.value)  # type: ignore[union-attr]
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(self.limiter.penalties[0], (SourceKey.HACKERNEWS, 10.0))

    async def test_malformed_payload_is_parse_error_after_other_windows(self) -> None:
        self.http.enqueue({"hits": "не список"})
        for _ in range(17):
            self.http.enqueue(page())
        _, failure = await self.collect()
        self.assertEqual(failure.code, AdapterErrorCode.PARSE_ERROR.value)  # type: ignore[union-attr]

    def test_helpers(self) -> None:
        self.assertEqual(hn_query('  "edge"   AI  '), "edge AI")
        self.assertEqual(story_text("<i>a</i>&lt;b&gt;"), "a <b>")
        self.assertIsNone(published_at({}))
        self.assertEqual(published_at({"created_at": "2026-01-02T03:04:05Z"}), datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
        self.assertEqual(HackerNewsAdapter.key, SourceKey.HACKERNEWS)
        self.assertEqual(HackerNewsAdapter.allowed_hosts, frozenset({"hn.algolia.com"}))


if __name__ == "__main__":
    unittest.main()
