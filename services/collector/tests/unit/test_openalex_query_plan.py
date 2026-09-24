"""OpenAlex: лимит документов делится между всеми фразами, препринты arXiv запрашиваются отдельно.

Дефект, который закрывают тесты (docs/quality/ERROR_ANALYSIS.md, E13): фразы обходились по очереди
до исчерпания лимита, русские шли первыми, и первая русская фраза за три страницы забирала все
150 документов. В отчётах это видно как ровно 3 запроса и 150 документов OpenAlex в каждом задании.
"""

from __future__ import annotations

import copy
import unittest

from collector.adapters.outbound.sources.openalex import ARXIV_SOURCE_ID, OpenAlexAdapter
from collector.domain.values import CollectionLimits, CollectionMode, SearchTerms

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter
from .test_sources import load_json

LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
TERMS = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic chips", "spiking processors"))
ARXIV = f"primary_location.source.id:{ARXIV_SOURCE_ID},publication_year:>{LIMITS.published_since_year - 1}"
ALL = f"publication_year:>{LIMITS.published_since_year - 1}"


def full_page(start: int, size: int) -> dict:
    """Полная страница ответа OpenAlex из `size` различных работ."""
    template = load_json("openalex/works_success.json")["results"][0]
    results = []
    for number in range(start, start + size):
        item = copy.deepcopy(template)
        item["id"] = f"https://openalex.org/W{number}"
        item["doi"] = f"https://doi.org/10.1000/work.{number}"
        item["title"] = f"Work number {number}"
        results.append(item)
    return {"meta": {"count": 10_000}, "results": results}


class QueryPlanTest(unittest.IsolatedAsyncioTestCase):
    """Каждая фраза получает свою долю лимита; английские и препринты arXiv идут первыми."""

    def setUp(self) -> None:
        self.http = FakeHttpClient()
        self.adapter = OpenAlexAdapter(self.http, FakeRateLimiter(), FakeClock(), api_key="key")

    async def collect(self) -> list:  # type: ignore[type-arg]
        return [document async for document in self.adapter.search(TERMS, LIMITS, 10_000.0)]

    async def test_first_phrase_no_longer_exhausts_budget(self) -> None:
        for page in range(40):
            self.http.enqueue(full_page(page * 100, 50))
        documents = await self.collect()
        queried = [(call[1]["params"]["search"], call[1]["params"]["filter"]) for call in self.http.calls]
        self.assertIn(("neuromorphic chips", ARXIV), queried)
        self.assertIn(("spiking processors", ALL), queried)
        self.assertIn(("нейроморфные чипы", ALL), queried, "русская фраза тоже запрашивается")
        self.assertEqual(len(documents), LIMITS.max_documents_per_source)

    async def test_order_arxiv_then_english_then_russian(self) -> None:
        for _ in range(5):
            self.http.enqueue(load_json("openalex/works_empty.json"))
        await self.collect()
        queried = [(call[1]["params"]["search"], call[1]["params"]["filter"]) for call in self.http.calls]
        self.assertEqual(queried, [
            ("neuromorphic chips", ARXIV), ("spiking processors", ARXIV),
            ("neuromorphic chips", ALL), ("spiking processors", ALL),
            ("нейроморфные чипы", ALL),
        ])

    async def test_each_query_gets_equal_share(self) -> None:
        for page in range(40):
            self.http.enqueue(full_page(page * 100, 30))
        documents = await self.collect()
        share = -(-LIMITS.max_documents_per_source // 5)
        per_page = {call[1]["params"]["per-page"] for call in self.http.calls}
        self.assertEqual(per_page, {min(50, share)})
        by_query: dict[tuple[str, str], int] = {}
        for document in documents:
            by_query[document.matched_term] = by_query.get(document.matched_term, 0) + 1
        self.assertLessEqual(max(by_query.values()), 2 * share, "английская фраза — не больше двух долей")
        self.assertIn("нейроморфные чипы", by_query)


if __name__ == "__main__":
    unittest.main()