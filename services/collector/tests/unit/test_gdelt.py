"""Адаптер GDELT на записанных ответах: окна истории, квота, таймауты, паузы перегрузки (этап 3).

Фикстура повторяет поля режима artlist. Живой формат и доступ к истории проверяются командой из инструкции этапа 3.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector.adapters.outbound.sources import gdelt
from collector.adapters.outbound.sources.gdelt import (
    GdeltAdapter,
    business_query,
    gdelt_datetime,
    history_windows,
    parse_seendate,
    records_per_request,
    reset_gdelt_gate,
)
from collector.application.ports import HttpTimeoutError
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import DEFAULT_POLICIES, SourcePolicy
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "gdelt" / "artlist_success.json"
TERMS = SearchTerms(ru=("роботы как услуга",), en=("robot as a service", "robot insurance", "skills marketplace"))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class GdeltTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_gdelt_gate()
        self.http = FakeHttpClient()
        self.clock = FakeClock(NOW)

    def tearDown(self) -> None:
        reset_gdelt_gate()

    def adapter(self) -> GdeltAdapter:
        adapter = GdeltAdapter(self.http, FakeRateLimiter(), self.clock, history_months=24)
        adapter.policy = DEFAULT_POLICIES[SourceKey.GDELT]
        return adapter

    async def collect(self, deadline: float = 10_000.0) -> tuple[list, AdapterFailure | None]:  # type: ignore[type-arg]
        documents, failure = [], None
        try:
            async for document in self.adapter().search(TERMS, LIMITS, deadline):
                documents.append(document)
        except AdapterFailure as error:
            failure = error
        return documents, failure

    def test_history_windows_cover_depth_by_90_days(self) -> None:
        windows = history_windows(NOW, 24, 2023)
        self.assertEqual(windows[0][1], NOW)
        self.assertTrue(all(a[0] == b[1] for a, b in zip(windows, windows[1:], strict=False)))  # окна смыкаются
        self.assertTrue(all(end - start <= timedelta(days=90) for start, end in windows))
        self.assertEqual(windows[-1][0], NOW - timedelta(days=round(24 * 30.44)))
        self.assertEqual(len(windows), 9)
        self.assertEqual(history_windows(NOW, 24, 2026)[-1][0], datetime(2026, 1, 1, tzinfo=UTC))  # не раньше since_year

    def test_records_split_quota_within_api_limits(self) -> None:
        self.assertEqual(records_per_request(150, 8), 19)
        self.assertEqual(records_per_request(150, 1), 150)
        self.assertEqual(records_per_request(5000, 1), 250)
        self.assertEqual(records_per_request(150, 100), 10)

    async def test_parses_articles_and_skips_invalid_and_old(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        for _ in range(7):
            self.http.enqueue({})
        documents, failure = await self.collect()
        self.assertIsNone(failure)
        self.assertEqual([d.url for d in documents], [
            "https://www.example-news.com/2026/08/robot-insurer-raises-seed", "https://www.example-wire.com/raas-pilot",
        ])
        first, second = documents
        self.assertEqual(first.language_code, "en")
        self.assertEqual(second.language_code, "ru")
        self.assertEqual(first.text, first.title)
        self.assertEqual(first.raw_meta, {"domain": "example-news.com", "source_country": "United States"})
        self.assertEqual(first.published_at.isoformat(), "2026-08-12T09:30:00+00:00")  # type: ignore[union-attr]

    async def test_requests_go_by_windows_newest_first_within_budget(self) -> None:
        for _ in range(8):
            self.http.enqueue({})
        _, failure = await self.collect()
        self.assertIsNone(failure)
        self.assertEqual(len(self.http.calls), 8)  # бюджет B2-b: 8 запросов
        params = [call[1]["params"] for call in self.http.calls]
        self.assertTrue(all(p["query"] == business_query("robot as a service") for p in params))  # первая фраза — все окна
        self.assertEqual({k: params[0][k] for k in ("mode", "format", "maxrecords")},
                         {"mode": "artlist", "format": "json", "maxrecords": 19})
        self.assertNotIn("timespan", params[0])
        self.assertEqual(params[0]["enddatetime"], gdelt_datetime(NOW))
        self.assertEqual(params[0]["startdatetime"], params[1]["enddatetime"])
        self.assertEqual(len(params[0]["startdatetime"]), 14)

    async def test_timeout_is_retried_once_and_other_windows_continue(self) -> None:
        self.http.enqueue(HttpTimeoutError("таймаут запроса"))
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        for _ in range(6):
            self.http.enqueue({})
        documents, failure = await self.collect()
        self.assertIsNone(failure)
        self.assertEqual(len(documents), 2)
        self.assertEqual(len(self.http.calls), 8)  # повтор окна тоже расходует бюджет
        self.assertIn(gdelt.MIN_GAP_SECONDS, [round(s, 1) for s in self.clock.slept])  # повтор — после интервала шлюза

    async def test_failed_window_is_reported_after_others(self) -> None:
        self.http.enqueue(HttpTimeoutError("таймаут запроса"))
        self.http.enqueue(HttpTimeoutError("таймаут запроса"))
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        for _ in range(5):
            self.http.enqueue({})
        documents, failure = await self.collect()
        self.assertEqual(len(documents), 2)  # документы других окон сохранены
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, AdapterErrorCode.TIMEOUT.value)  # type: ignore[union-attr]
        self.assertIn("1 из 7", failure.message)  # type: ignore[union-attr]

    async def test_timeout_does_not_pause_next_collection(self) -> None:
        self.http.enqueue(HttpTimeoutError("таймаут"))
        self.http.enqueue(HttpTimeoutError("таймаут"))
        for _ in range(6):
            self.http.enqueue({})
        await self.collect()
        calls = len(self.http.calls)
        for _ in range(8):
            self.http.enqueue({})
        _, failure = await self.collect()
        self.assertIsNone(failure)
        self.assertEqual(len(self.http.calls), calls + 8)  # следующий сбор опрашивает GDELT сразу

    async def test_rate_limit_stops_collection_and_short_pause_blocks_only_until_it_ends(self) -> None:
        self.http.enqueue(status_error(429))
        documents, failure = await self.collect(deadline=self.clock.monotonic() + 100)
        self.assertEqual(documents, [])
        self.assertEqual(failure.code, AdapterErrorCode.RATE_LIMITED.value)  # type: ignore[union-attr]
        self.assertEqual(len(self.http.calls), 1)
        _, blocked = await self.collect(deadline=self.clock.monotonic() + 100)
        self.assertEqual(len(self.http.calls), 1)  # пауза 120 с длиннее остатка сбора — без запросов
        self.assertIn("пауза после перегрузки", blocked.message)  # type: ignore[union-attr]
        self.clock.advance(gdelt.COOLDOWN_SECONDS + 1)
        for _ in range(8):
            self.http.enqueue({})
        _, failure = await self.collect()
        self.assertIsNone(failure)

    async def test_pause_ending_before_deadline_is_awaited(self) -> None:
        self.http.enqueue(status_error(429))
        await self.collect(deadline=self.clock.monotonic() + 100)
        for _ in range(8):
            self.http.enqueue({})
        _, failure = await self.collect(deadline=self.clock.monotonic() + 1000)
        self.assertIsNone(failure)
        self.assertTrue(any(s >= gdelt.COOLDOWN_SECONDS - 1 for s in self.clock.slept))

    async def test_non_json_answer_is_parse_error_with_pause(self) -> None:
        self.http.enqueue("Please limit requests to one every 5 seconds")
        _, failure = await self.collect(deadline=self.clock.monotonic() + 100)
        self.assertEqual(failure.code, AdapterErrorCode.PARSE_ERROR.value)  # type: ignore[union-attr]
        self.assertGreater(gdelt._GATE.blocked_until, self.clock.monotonic())

    async def test_empty_answer_is_nothing_found(self) -> None:
        for _ in range(8):
            self.http.enqueue({})
        documents, failure = await self.collect()
        self.assertEqual(documents, [])
        self.assertIsNone(failure)

    def test_business_query_escapes_quotes(self) -> None:
        self.assertEqual(business_query('robot "as" a\\service'),
                         '"robot as a service" (pilot OR funding OR startup OR contract OR launch OR partnership OR deployment) '
                         'sourcelang:english')

    def test_seendate(self) -> None:
        self.assertEqual(parse_seendate("20260923T120000Z"), datetime(2026, 9, 23, 12, 0, tzinfo=UTC))
        self.assertIsNone(parse_seendate("2026-09-23"))

    def test_policy_default(self) -> None:
        self.assertEqual(DEFAULT_POLICIES[SourceKey.GDELT], SourcePolicy(max_requests=8, transient_retries=0, cooldown_seconds=1800.0))
        self.assertEqual(GdeltAdapter.policy, DEFAULT_POLICIES[SourceKey.GDELT])  # этап 4.2: запросы GDELT прежние
        self.assertEqual(GdeltAdapter.key, SourceKey.GDELT)


if __name__ == "__main__":
    unittest.main()
