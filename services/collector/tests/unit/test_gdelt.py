"""Адаптер GDELT на записанных ответах: разбор, запрос с деловыми маркерами, пауза после отказа.

Фикстура повторяет поля режима artlist; живой формат проверяется командой из COPY_PASTE_GUIDE_MACOS_RU.md.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from collector.adapters.outbound.sources import gdelt
from collector.adapters.outbound.sources.gdelt import GdeltAdapter, business_query, parse_seendate, reset_gdelt_gate
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "gdelt" / "artlist_success.json"
TERMS = SearchTerms(ru=("роботы как услуга",), en=("robot as a service", "robot insurance", "skills marketplace"))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)


class GdeltTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_gdelt_gate()
        self.http = FakeHttpClient()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        reset_gdelt_gate()

    async def collect(self) -> list:  # type: ignore[type-arg]
        adapter = GdeltAdapter(self.http, FakeRateLimiter(), self.clock)
        return [document async for document in adapter.search(TERMS, LIMITS, 10_000.0)]

    async def test_parses_articles_and_skips_invalid_and_old(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.http.enqueue({})
        documents = await self.collect()
        self.assertEqual([d.url for d in documents], [
            "https://www.example-news.com/2026/08/robot-insurer-raises-seed", "https://www.example-wire.com/raas-pilot",
        ])
        first, second = documents
        self.assertEqual(first.language_code, "en")
        self.assertEqual(second.language_code, "ru")
        self.assertEqual(first.text, first.title)
        self.assertEqual(first.raw_meta, {"domain": "example-news.com", "source_country": "United States"})
        self.assertEqual(first.published_at.isoformat(), "2026-08-12T09:30:00+00:00")  # type: ignore[union-attr]

    async def test_request_parameters_and_two_english_phrases(self) -> None:
        self.http.enqueue({})
        self.http.enqueue({})
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)
        url, request = self.http.calls[0]
        self.assertEqual(url, gdelt.API_URL)
        params = request["params"]
        self.assertEqual({k: params[k] for k in ("mode", "format", "maxrecords", "timespan")},
                         {"mode": "artlist", "format": "json", "maxrecords": 75, "timespan": "3months"})
        self.assertTrue(params["query"].startswith('"robot as a service" (pilot OR funding'))
        self.assertIn('"robot insurance"', self.http.calls[1][1]["params"]["query"])

    def test_business_query_escapes_quotes(self) -> None:
        self.assertEqual(business_query('robot "insurance"  \\ market')[:28], '"robot insurance market" (pi')
        self.assertTrue(business_query("x").endswith("sourcelang:english"))

    def test_seendate(self) -> None:
        self.assertIsNone(parse_seendate("2026-09-01"))
        self.assertEqual(parse_seendate("20260901T140000Z").year, 2026)  # type: ignore[union-attr]

    async def test_empty_answer_is_nothing_found(self) -> None:
        self.http.enqueue({})
        self.http.enqueue({})
        self.assertEqual(await self.collect(), [])

    async def test_cooldown_after_rate_limit(self) -> None:
        self.http.enqueue(status_error(429))
        with self.assertRaises(AdapterFailure):
            await self.collect()
        with self.assertRaises(AdapterFailure) as second:
            await self.collect()
        self.assertIn("пауза", second.exception.message)
        self.assertEqual(len(self.http.calls), 1)

    async def test_non_json_answer_is_parse_error_with_cooldown(self) -> None:
        self.http.enqueue(["не объект"])
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.PARSE_ERROR.value)
        with self.assertRaises(AdapterFailure):
            await self.collect()
        self.assertEqual(len(self.http.calls), 1)

    def test_source_key(self) -> None:
        self.assertIs(GdeltAdapter(FakeHttpClient(), FakeRateLimiter(), FakeClock()).key, SourceKey.GDELT)


if __name__ == "__main__":
    unittest.main()
