"""Адаптеры источников на записанных ответах (`tests/fixtures/http`), без сети."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from collector.adapters.outbound.sources.arxiv import ArxivAdapter, reset_arxiv_gate
from collector.adapters.outbound.sources.base import parse_datetime
from collector.adapters.outbound.sources.github import GithubAdapter
from collector.adapters.outbound.sources.hh import HhAdapter
from collector.adapters.outbound.sources.openalex import OpenAlexAdapter, restore_abstract
from collector.adapters.outbound.sources.patentsview import PatentsViewAdapter
from collector.adapters.outbound.sources.rss import RssAdapter
from collector.adapters.outbound.sources.wikipedia import WikipediaProbe
from collector.application.ports import HttpPayloadError, HttpTimeoutError, HttpTransportError
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "http"
TERMS = SearchTerms(ru=(), en=("neuromorphic chips",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
DEADLINE = 10_000.0


def load_json(name: str) -> object:
    """Кассета JSON."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    """Кассета XML."""
    return (FIXTURES / name).read_text(encoding="utf-8")


async def collect(adapter, terms=TERMS, limits=LIMITS):  # noqa: ANN001, ANN201 - помощник тестов
    """Собирает всё, что выдал адаптер."""
    return [document async for document in adapter.search(terms, limits, DEADLINE)]


class OpenAlexTest(unittest.IsolatedAsyncioTestCase):
    """Разбор ответа OpenAlex и обработка отказов."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()
        self.adapter = OpenAlexAdapter(self.http, self.limiter, FakeClock(), api_key="secret-key")

    async def test_parses_documents(self) -> None:
        self.http.enqueue(load_json("openalex/works_success.json"))
        self.http.enqueue(load_json("openalex/works_empty.json"))
        documents = await collect(self.adapter)
        self.assertEqual(len(documents), 2)
        first = documents[0]
        self.assertEqual(first.url, "https://dl.acm.org/doi/10.1145/3658644.3670123")
        self.assertIn("certification method", first.text)
        self.assertEqual(first.citation_count, 7)
        self.assertEqual(first.doi, "https://doi.org/10.1145/3658644.3670123")
        self.assertEqual(first.raw_meta["type"], "article")
        self.assertEqual(first.published_at, parse_datetime("2026-03-11"))
        self.assertEqual(documents[1].url, "https://openalex.org/W4392002222")

    async def test_api_key_and_filter_passed(self) -> None:
        self.http.enqueue(load_json("openalex/works_empty.json"))
        self.http.enqueue(load_json("openalex/works_empty.json"))
        await collect(self.adapter)
        params = self.http.calls[0][1]["params"]
        self.assertEqual(params["api_key"], "secret-key")
        self.assertEqual(params["filter"], "primary_location.source.id:S4306400194,publication_year:>2022")
        self.assertEqual(self.http.calls[1][1]["params"]["filter"], "publication_year:>2022")
        self.assertEqual(self.limiter.acquired[0].value, "openalex")

    async def test_empty_response(self) -> None:
        self.http.enqueue(load_json("openalex/works_empty.json"))
        self.http.enqueue(load_json("openalex/works_empty.json"))
        self.assertEqual(await collect(self.adapter), [])

    async def test_malformed_response_is_parse_error(self) -> None:
        self.http.enqueue(load_json("openalex/works_malformed.json"))
        with self.assertRaises(AdapterFailure) as ctx:
            await collect(self.adapter)
        self.assertEqual(ctx.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    async def test_missing_api_key_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            OpenAlexAdapter(self.http, self.limiter, FakeClock(), api_key="")

    async def test_per_source_limit_respected(self) -> None:
        self.http.enqueue(load_json("openalex/works_success.json"))
        limits = CollectionLimits(1, 800, 120, 2023)
        self.assertEqual(len(await collect(self.adapter, limits=limits)), 1)

    def test_restore_abstract(self) -> None:
        self.assertEqual(restore_abstract({"b": [1], "a": [0], "c": [2, 3]}), "a b c c")
        self.assertEqual(restore_abstract(None), "")
        self.assertEqual(restore_abstract({}), "")


class AdapterErrorMappingTest(unittest.IsolatedAsyncioTestCase):
    """Перевод ошибок транспорта в коды §10.7."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()
        self.adapter = OpenAlexAdapter(self.http, self.limiter, FakeClock(), api_key="k")

    async def assert_code(self, response: Exception, expected: AdapterErrorCode) -> AdapterFailure:
        self.http.enqueue(response)
        with self.assertRaises(AdapterFailure) as ctx:
            await collect(self.adapter)
        self.assertEqual(ctx.exception.code, expected.value)
        return ctx.exception

    async def test_timeout(self) -> None:
        await self.assert_code(HttpTimeoutError("timeout"), AdapterErrorCode.TIMEOUT)

    async def test_client_error(self) -> None:
        await self.assert_code(status_error(403), AdapterErrorCode.HTTP_4XX)

    async def test_server_error(self) -> None:
        await self.assert_code(status_error(500), AdapterErrorCode.HTTP_5XX)

    async def test_transport_error(self) -> None:
        await self.assert_code(HttpTransportError("dns"), AdapterErrorCode.HTTP_5XX)

    async def test_payload_error(self) -> None:
        await self.assert_code(HttpPayloadError("too big"), AdapterErrorCode.PARSE_ERROR)

    async def test_single_429_penalizes_limiter(self) -> None:
        await self.assert_code(status_error(429, retry_after=12.0), AdapterErrorCode.RATE_LIMITED)
        self.assertEqual(self.limiter.penalties, [(self.adapter.key, 12.0)])

    async def test_three_consecutive_429_report_rate_limited(self) -> None:
        for _ in range(2):
            self.http.enqueue(status_error(429, retry_after=1.0))
            with self.assertRaises(AdapterFailure):
                await collect(self.adapter)
        failure = await self.assert_code(status_error(429, 1.0), AdapterErrorCode.RATE_LIMITED)
        self.assertIn("3 ответа 429 подряд", failure.message)
        self.assertEqual(len(self.limiter.penalties), 3)

    async def test_http_requests_counted(self) -> None:
        self.http.enqueue(status_error(500))
        with self.assertRaises(AdapterFailure):
            await collect(self.adapter)
        self.assertEqual(self.adapter.http_requests, 1)


class ArxivTest(unittest.IsolatedAsyncioTestCase):
    """Разбор Atom-ответа arXiv."""

    def setUp(self) -> None:
        reset_arxiv_gate()
        self.http = FakeHttpClient()
        self.adapter = ArxivAdapter(self.http, FakeRateLimiter(), FakeClock())

    async def test_parses_and_filters_by_year(self) -> None:
        self.http.enqueue(load_text("arxiv/query_success.xml"))
        documents = await collect(self.adapter)
        self.assertEqual(len(documents), 1)  # вторая запись отсеяна фильтром published_since_year
        document = documents[0]
        self.assertEqual(document.url, "http://arxiv.org/abs/2603.01234v1")
        self.assertEqual(document.doi, "10.48550/arXiv.2603.01234")
        self.assertEqual(document.raw_meta["categories"], ["cs.CR", "cs.AR"])
        self.assertEqual(document.language_code, "en")

    async def test_broken_xml_is_parse_error(self) -> None:
        self.http.enqueue(load_text("arxiv/query_broken.xml"))
        with self.assertRaises(AdapterFailure) as ctx:
            await collect(self.adapter)
        self.assertEqual(ctx.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    async def test_query_uses_significant_words(self) -> None:
        # 27.09: точная фраза дала 0 документов в 5 темах из 6 — слова фразы ищутся через AND.
        self.http.enqueue(load_text("arxiv/query_success.xml"))
        await collect(self.adapter)
        self.assertEqual(self.http.calls[0][1]["params"]["search_query"], "all:neuromorphic AND all:chips")


class RssTest(unittest.IsolatedAsyncioTestCase):
    """Отбор релевантных записей лент RSS и Atom."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.adapter = RssAdapter(
            self.http,
            FakeRateLimiter(),
            FakeClock(),
            feeds=["https://www.cnews.ru/inc/rss.xml", "https://arstechnica.com/feed/"],
        )

    async def test_relevance_filter_and_parsing(self) -> None:
        self.http.enqueue(load_text("rss/feed_rss.xml"))
        self.http.enqueue(load_text("rss/feed_atom.xml"))
        terms = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic edge",))
        documents = await collect(self.adapter, terms=terms)
        urls = [document.url for document in documents]
        self.assertIn("https://www.cnews.ru/news/top/neuromorphic_edge?utm_source=rss&utm_medium=feed", urls)
        self.assertIn("https://arstechnica.com/2026/03/neuromorphic-edge/", urls)
        self.assertNotIn("https://www.cnews.ru/news/top/agro", urls)
        self.assertEqual(documents[0].matched_term, "нейроморфные чипы")
        self.assertEqual(documents[0].raw_meta["feed"], "CNews: технологии")

    async def test_allowed_hosts_from_feed_list(self) -> None:
        self.assertEqual(self.adapter.allowed_hosts, frozenset({"www.cnews.ru", "arstechnica.com"}))

    async def test_feed_failure_does_not_stop_others(self) -> None:
        self.http.enqueue(status_error(500))
        self.http.enqueue(load_text("rss/feed_atom.xml"))
        documents = await collect(self.adapter, terms=SearchTerms(en=("neuromorphic edge",)))
        self.assertEqual(len(documents), 1)

    async def test_empty_feed_list_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RssAdapter(self.http, FakeRateLimiter(), FakeClock(), feeds=[])


class GithubTest(unittest.IsolatedAsyncioTestCase):
    """Разбор ответа GitHub Search."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.adapter = GithubAdapter(self.http, FakeRateLimiter(), FakeClock(), token="ghp-test")

    async def test_parses_repositories(self) -> None:
        self.http.enqueue(load_json("github/search_success.json"))
        documents = await collect(self.adapter)
        self.assertEqual(len(documents), 1)  # запись без html_url отброшена
        document = documents[0]
        self.assertEqual(document.url, "https://github.com/edge-ai/neuro-runtime")
        self.assertEqual(document.engagement_count, 412)
        self.assertIn("neuromorphic", document.text)
        self.assertEqual(document.raw_meta["language"], "Rust")

    async def test_token_and_query(self) -> None:
        self.http.enqueue(load_json("github/search_success.json"))
        await collect(self.adapter)
        call = self.http.calls[0][1]
        self.assertEqual(call["headers"]["Authorization"], "Bearer ghp-test")
        self.assertEqual(call["params"]["q"], "neuromorphic chips created:>2023-01-01")

    async def test_works_without_token(self) -> None:
        adapter = GithubAdapter(self.http, FakeRateLimiter(), FakeClock())
        self.http.enqueue(load_json("github/search_success.json"))
        await collect(adapter)
        self.assertNotIn("Authorization", self.http.calls[0][1]["headers"])


class WikipediaTest(unittest.IsolatedAsyncioTestCase):
    """Проверка статьи, просмотров и даты создания."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.probe = WikipediaProbe(self.http, FakeRateLimiter(), FakeClock())

    async def test_existing_article(self) -> None:
        self.http.enqueue(load_json("wikipedia/summary_found.json"))
        self.http.enqueue(load_json("wikipedia/pageviews.json"))
        self.http.enqueue(load_json("wikipedia/revisions.json"))
        hit = await self.probe.probe("Kubernetes", "en")
        self.assertTrue(hit.exists)
        self.assertEqual(hit.page_url, "https://en.wikipedia.org/wiki/Kubernetes")
        self.assertEqual(hit.pageviews_30d, 2540)
        self.assertEqual(hit.created_at, parse_datetime("2014-07-21T18:00:00Z"))

    async def test_missing_article(self) -> None:
        self.http.enqueue(status_error(404))
        hit = await self.probe.probe("Неизвестная технология", "ru")
        self.assertFalse(hit.exists)
        self.assertEqual(hit.pageviews_30d, -1)

    async def test_pageviews_unavailable_returns_minus_one(self) -> None:
        self.http.enqueue(load_json("wikipedia/summary_found.json"))
        self.http.enqueue(status_error(500))
        self.http.enqueue(status_error(500))
        hit = await self.probe.probe("Kubernetes", "en")
        self.assertTrue(hit.exists)
        self.assertEqual(hit.pageviews_30d, -1)
        self.assertIsNone(hit.created_at)

    async def test_search_is_not_supported(self) -> None:
        with self.assertRaises(AdapterFailure) as ctx:
            self.probe.search(TERMS, LIMITS, DEADLINE)
        self.assertEqual(ctx.exception.code, AdapterErrorCode.DISABLED.value)


class DisabledSourcesTest(unittest.TestCase):
    """Каркасы отключённых источников не выдают данных."""

    def test_disabled_adapters_report_code(self) -> None:
        clock, http, limiter = FakeClock(), FakeHttpClient(), FakeRateLimiter()
        for adapter in (
            PatentsViewAdapter(http, limiter, clock),
            HhAdapter(http, limiter, clock),
        ):
            with self.subTest(adapter=adapter.key), self.assertRaises(AdapterFailure) as ctx:
                adapter.search(TERMS, LIMITS, DEADLINE)
            self.assertEqual(ctx.exception.code, AdapterErrorCode.DISABLED.value)


class DateParsingTest(unittest.TestCase):
    """Разбор дат публикации в разных форматах."""

    def test_formats(self) -> None:
        self.assertIsNotNone(parse_datetime("2026-03-11"))
        self.assertIsNotNone(parse_datetime("2026-03-02T17:21:40Z"))
        self.assertIsNotNone(parse_datetime("Mon, 09 Mar 2026 08:30:00 +0300"))
        self.assertIsNone(parse_datetime("вчера"))
        self.assertIsNone(parse_datetime(None))

    def test_timezone_normalized_to_utc(self) -> None:
        moment = parse_datetime("Mon, 09 Mar 2026 08:30:00 +0300")
        assert moment is not None
        self.assertEqual(moment.hour, 5)


if __name__ == "__main__":
    unittest.main()
