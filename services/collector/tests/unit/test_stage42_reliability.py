"""Этап 4.2: один владелец повторов, квотный 403, частоты по правилам сервисов, RSS, редиректы, извлечение."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from urllib.parse import urljoin

from collector.adapters.outbound.sources.base import quota_exhausted_wait
from collector.adapters.outbound.sources.hackernews import HackerNewsAdapter
from collector.adapters.outbound.sources.rss import FEED_ACCEPT, RssAdapter, parse_feed
from collector.application.ports import HttpStatusError, HttpTransportError
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import (
    GITHUB_SEARCH_ANON_RPS,
    effective_rates,
    parse_source_policies,
)
from collector.domain.technology_mining import candidate_phrases, mine_technologies, phrase_key
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
RSS_ITEM = """<?xml version="1.0"?><rss version="2.0"><channel><title>Tech</title>
<item><title>Startup bets on solid-state transformers for the grid</title><link>https://example.org/sst</link>
<description>Pilot of solid-state transformers.</description><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
RDF_FEED = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>RDF feed</title></channel>
<item><title>Edge NPU benchmark</title><link>https://example.org/npu</link><description>NPU results</description>
<dc:date>2026-10-05T10:00:00Z</dc:date><dc:subject>edge</dc:subject></item></rdf:RDF>"""


class RetryOwnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()
        self.adapter = HackerNewsAdapter(self.http, self.limiter, FakeClock())

    async def test_transient_error_retried_once_through_limiter(self) -> None:
        self.http.enqueue(status_error(503))
        self.http.enqueue({"hits": []})
        payload = await self.adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "x"})
        self.assertEqual(payload, {"hits": []})
        self.assertEqual(self.adapter.http_requests, 2)
        self.assertEqual(self.limiter.acquired, [SourceKey.HACKERNEWS, SourceKey.HACKERNEWS])  # каждая попытка — токен
        self.assertEqual(self.limiter.deferred, [(SourceKey.HACKERNEWS, 2.0)])

    async def test_network_error_is_not_5xx_and_forbidden_redirect_not_retried(self) -> None:
        self.http.enqueue(HttpTransportError("хост evil.example не разрешён для этого адаптера"))
        with self.assertRaises(AdapterFailure) as ctx:
            await self.adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "x"})
        self.assertEqual(ctx.exception.code, AdapterErrorCode.NETWORK_ERROR.value)
        self.assertEqual(self.adapter.http_requests, 1)

    async def test_quota_403_is_rate_limited_with_pause_until_reset(self) -> None:
        self.adapter.policy = parse_source_policies("hackernews:retries=0")[SourceKey.HACKERNEWS]
        reset = 2_000_000_000
        self.http.enqueue(HttpStatusError(403, "квота", None, None, {"x-ratelimit-remaining": "0",
                                                                      "x-ratelimit-reset": str(reset)}))
        with self.assertRaises(AdapterFailure) as ctx:
            await self.adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "x"})
        self.assertEqual(ctx.exception.code, AdapterErrorCode.RATE_LIMITED.value)
        self.assertEqual(len(self.limiter.penalties), 1)

    async def test_plain_403_is_client_error_without_retry(self) -> None:
        self.http.enqueue(status_error(403))
        with self.assertRaises(AdapterFailure) as ctx:
            await self.adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "x"})
        self.assertEqual(ctx.exception.code, AdapterErrorCode.HTTP_4XX.value)
        self.assertEqual((self.adapter.http_requests, self.limiter.penalties), (1, []))

    def test_quota_wait(self) -> None:
        now = 1_000.0
        self.assertEqual(quota_exhausted_wait(HttpStatusError(403, "x", 30.0), now), 30.0)
        self.assertIsNone(quota_exhausted_wait(HttpStatusError(403, "x", None, None, {"x-ratelimit-remaining": "3"}), now))
        self.assertEqual(quota_exhausted_wait(HttpStatusError(403, "x", None, None, {"x-ratelimit-remaining": "0",
                                                                                     "x-ratelimit-reset": "1045"}), now), 45.0)
        self.assertEqual(quota_exhausted_wait(HttpStatusError(403, "x", None, None, {"x-ratelimit-remaining": "0",
                                                                                     "x-ratelimit-reset": "999999"}), now), 120.0)
        self.assertIsNone(quota_exhausted_wait(HttpStatusError(404, "x"), now))


class RatesAndPoliciesTest(unittest.TestCase):
    def test_rates_capped_by_service_rules(self) -> None:
        catalog = {SourceKey.GITHUB: 0.45, SourceKey.SEMANTIC_SCHOLAR: 0.5, SourceKey.WIKIPEDIA: 10.0,
                   SourceKey.ARXIV: 0.33, SourceKey.ZENODO: 1.0, SourceKey.OPENALEX: 5.0, SourceKey.RSS: 2.0}
        anon = effective_rates(catalog, github_token=False, semantic_scholar_key=False)
        self.assertEqual(anon[SourceKey.GITHUB], GITHUB_SEARCH_ANON_RPS)  # 10 в минуту без токена → раз в 7 с
        self.assertEqual((anon[SourceKey.WIKIPEDIA], anon[SourceKey.ARXIV], anon[SourceKey.ZENODO]), (2.0, 0.3, 0.4))
        self.assertEqual((anon[SourceKey.OPENALEX], anon[SourceKey.RSS], anon[SourceKey.SEMANTIC_SCHOLAR]), (5.0, 2.0, 0.5))
        keyed = effective_rates(catalog, github_token=True, semantic_scholar_key=True)
        self.assertEqual((keyed[SourceKey.GITHUB], keyed[SourceKey.SEMANTIC_SCHOLAR]), (0.45, 0.5))  # каталог ниже границы
        self.assertEqual(effective_rates({SourceKey.ARXIV: 0.1}, github_token=False, semantic_scholar_key=False),
                         {SourceKey.ARXIV: 0.1})

    def test_policy_parser_knows_transient(self) -> None:
        policies = parse_source_policies("openalex:transient=2;gdelt:requests=4")
        self.assertEqual(policies[SourceKey.OPENALEX].transient_retries, 2)
        self.assertEqual(policies[SourceKey.GDELT].transient_retries, 0)  # не сбрасывается к умолчанию


class RssTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()

    def adapter(self, *feeds: str) -> RssAdapter:
        return RssAdapter(self.http, FakeRateLimiter(), FakeClock(), feeds=list(feeds))

    async def test_accept_header_and_technology_match(self) -> None:
        self.http.enqueue(RSS_ITEM)
        terms = SearchTerms(en=("power grid digitalization",), technologies=("solid-state transformers",))
        documents = [d async for d in self.adapter("https://example.org/feed").search(terms, LIMITS, 10_000.0)]
        self.assertEqual(self.http.calls[0][1]["headers"], FEED_ACCEPT)
        self.assertEqual([d.matched_term for d in documents], ["solid-state transformers"])

    async def test_all_feeds_failed_is_a_failure_not_empty_result(self) -> None:
        self.http.enqueue(status_error(404))
        self.http.enqueue(status_error(404))
        with self.assertRaises(AdapterFailure) as ctx:
            _ = [d async for d in self.adapter("https://a.example/feed", "https://b.example/feed").search(
                SearchTerms(en=("x y",)), LIMITS, 10_000.0)]
        self.assertIn("все 2 лент недоступны", ctx.exception.message)

    def test_rdf_feed_and_non_feed_root(self) -> None:
        entries = parse_feed(RDF_FEED, "https://example.org/rdf")
        self.assertEqual((entries[0].title, entries[0].link, entries[0].feed_title),
                         ("Edge NPU benchmark", "https://example.org/npu", "RDF feed"))
        self.assertIsNotNone(entries[0].published_at)
        with self.assertRaises(AdapterFailure) as ctx:
            parse_feed("<html><body>challenge</body></html>", "https://example.org/feed")
        self.assertEqual(ctx.exception.code, AdapterErrorCode.PARSE_ERROR.value)


class MiningRulesTest(unittest.TestCase):
    def test_segments_prefixes_generic_and_non_english(self) -> None:
        found = candidate_phrases("Show HN: Model card for time series forecasting – de la nube")
        self.assertNotIn("card for time", found)
        self.assertFalse(any("hn" in phrase.split() for phrase in found))
        self.assertFalse(any("de" in phrase.split() for phrase in found))
        self.assertEqual(candidate_phrases("AI agents in the age of AI"), set())
        self.assertEqual(phrase_key("optical interconnects"), "optical interconnect")

    def test_two_sources_and_plural_merge(self) -> None:
        titles = [("Optical interconnect startup raises funds", "rss"), ("Optical interconnects for GPU fabrics", "arxiv"),
                  ("Supply chain finance with agentic AI", "rss"), ("Agentic AI for supply chain finance", "openalex"),
                  ("Thermal interface gel study", "arxiv"), ("Thermal interface gel results", "arxiv")]
        titles += [(f"Filler {i}", "rss") for i in range(6)]
        self.assertEqual(mine_technologies(titles), ["optical interconnect"])  # один источник — не технология


class RedirectTest(unittest.IsolatedAsyncioTestCase):
    """Редиректы HTTP-клиента: хост каждого перехода проверяется, ключ не уходит на чужой origin."""

    @classmethod
    def setUpClass(cls) -> None:
        if importlib.util.find_spec("httpx") is None:  # песочница без httpx: заглушка только для импорта модуля
            stub = types.ModuleType("httpx")

            class _Error(Exception):
                pass

            stub.HTTPError = _Error
            stub.TimeoutException = type("TimeoutException", (_Error,), {})
            stub.Timeout = lambda *a, **k: None
            stub.Limits = lambda *a, **k: None
            stub.AsyncClient = lambda *a, **k: None
            sys.modules["httpx"] = stub
        from collector.adapters.outbound.http_client import HttpxClient

        cls.client_class = HttpxClient

    def make(self, routes: dict[str, tuple[int, dict[str, str], bytes]]):  # noqa: ANN201
        client = self.client_class(contact_email="ops@example.org", read_timeout_seconds=5, max_bytes=10_000, retries=0)
        sent: list[tuple[str, dict[str, str]]] = []

        class _URL(str):
            def join(self, location: str) -> "_URL":
                return _URL(urljoin(str(self), location))

        class _Headers(dict):
            def get(self, name, default=None):  # noqa: ANN001, ANN201
                return super().get(name.lower(), default)

        class _Response:
            def __init__(self, url: str, status: int, headers: dict[str, str], body: bytes) -> None:
                self.url, self.status_code = _URL(url), status
                self.headers = _Headers({k.lower(): v for k, v in headers.items()})
                self._body = body

            async def aiter_bytes(self):  # noqa: ANN201
                yield self._body

            async def aclose(self) -> None:
                return None

        class _Client:
            def build_request(self, method, url, params=None, headers=None, json=None):  # noqa: ANN001, ANN201
                return (url, dict(headers or {}))

            async def send(self, request, stream=True):  # noqa: ANN001, ANN201
                url, headers = request
                sent.append((url, headers))
                status, extra, body = routes[url]
                return _Response(url, status, extra, body)

        client._client = _Client()
        return client, sent

    async def test_same_host_redirect_followed(self) -> None:
        client, sent = self.make({
            "https://feeds.example.org/rss": (301, {"Location": "/rss/"}, b""),
            "https://feeds.example.org/rss/": (200, {}, b"<rss/>"),
        })
        body = await client.get_text("https://feeds.example.org/rss", allowed_hosts=frozenset({"example.org"}))
        self.assertEqual(body, "<rss/>")
        self.assertEqual([url for url, _ in sent], ["https://feeds.example.org/rss", "https://feeds.example.org/rss/"])

    async def test_redirect_to_foreign_host_rejected(self) -> None:
        client, _sent = self.make({"https://api.example.org/x": (302, {"Location": "https://evil.example/x"}, b"")})
        with self.assertRaises(HttpTransportError):
            await client.get_text("https://api.example.org/x", allowed_hosts=frozenset({"api.example.org"}))

    async def test_key_not_sent_to_other_origin_and_quota_headers_kept(self) -> None:
        client, sent = self.make({
            "https://a.example.org/s": (307, {"Location": "https://b.example.org/s"}, b""),
            "https://b.example.org/s": (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1791376330"}, b"{}"),
        })
        with self.assertRaises(HttpStatusError) as ctx:
            await client.get_text("https://a.example.org/s", allowed_hosts=frozenset({"example.org"}),
                                  headers={"x-api-key": "SECRET"})
        self.assertEqual(sent[0][1], {"x-api-key": "SECRET"})
        self.assertEqual(sent[1][1], {})
        self.assertEqual(ctx.exception.rate_headers["x-ratelimit-remaining"], "0")


if __name__ == "__main__":
    unittest.main()
