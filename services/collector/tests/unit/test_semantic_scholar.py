"""Адаптер Semantic Scholar на записанном ответе, без сети."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from collector.adapters.outbound.sources.semantic_scholar import SemanticScholarAdapter
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "semantic_scholar" / "search_success.json"
TERMS = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic chips",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
DEADLINE = 10_000.0


async def collect(adapter: SemanticScholarAdapter, terms: SearchTerms = TERMS) -> list:  # type: ignore[type-arg]
    """Всё, что выдал адаптер."""
    return [document async for document in adapter.search(terms, LIMITS, DEADLINE)]


class SemanticScholarTest(unittest.IsolatedAsyncioTestCase):
    """Разбор ответа, ключ в заголовке и обработка некорректного ответа."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()

    async def test_parses_documents_and_skips_empty(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        documents = await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock()))
        self.assertEqual(len(documents), 2)
        first, second = documents
        self.assertIn("MXene", first.title)
        self.assertEqual(first.doi, "https://doi.org/10.1000/xyz.2026.001")
        self.assertEqual(first.citation_count, 4)
        self.assertEqual(first.raw_meta["arxiv_id"], "2605.01234")
        self.assertEqual(first.matched_term, "neuromorphic chips")
        self.assertEqual(second.url, "https://www.semanticscholar.org/paper/f6g7h8i9j0")
        self.assertEqual(second.text, "")
        self.assertIsNotNone(second.published_at)

    async def test_english_phrases_only(self) -> None:
        self.http.enqueue({"data": []})
        await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock()))
        queries = [call[1]["params"]["query"] for call in self.http.calls]
        self.assertEqual(queries, ["neuromorphic chips"])

    async def test_api_key_sent_as_header(self) -> None:
        self.http.enqueue({"data": []})
        await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock(), api_key="secret"))
        self.assertEqual(self.http.calls[0][1]["headers"].get("x-api-key"), "secret")
        self.assertNotIn("secret", json.dumps(self.http.calls[0][1]["params"]))

    async def test_no_key_no_header(self) -> None:
        self.http.enqueue({"data": []})
        await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock()))
        self.assertNotIn("x-api-key", self.http.calls[0][1]["headers"])

    async def test_year_filter_from_limits(self) -> None:
        self.http.enqueue({"data": []})
        await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock()))
        self.assertEqual(self.http.calls[0][1]["params"]["year"], f"{LIMITS.published_since_year}-")

    async def test_malformed_payload_is_parse_error(self) -> None:
        self.http.enqueue({"data": "не список"})
        with self.assertRaises(AdapterFailure) as error:
            await collect(SemanticScholarAdapter(self.http, self.limiter, FakeClock()))
        self.assertEqual(error.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    def test_source_key(self) -> None:
        adapter = SemanticScholarAdapter(self.http, self.limiter, FakeClock())
        self.assertIs(adapter.key, SourceKey.SEMANTIC_SCHOLAR)


if __name__ == "__main__":
    unittest.main()