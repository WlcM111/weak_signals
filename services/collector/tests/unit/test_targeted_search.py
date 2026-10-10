"""Этап 4: извлечение технологий, прицельный поиск адаптеров, двухпроходный сбор."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.adapters.outbound.sources.arxiv import ArxivAdapter, phrase_query, reset_arxiv_gate
from collector.adapters.outbound.sources.github import MAX_TARGETED_TERMS, GithubAdapter
from collector.adapters.outbound.sources.hackernews import HackerNewsAdapter
from collector.adapters.outbound.sources.openalex import OpenAlexAdapter
from collector.adapters.outbound.sources.semantic_scholar import RELEVANCE_URL, SemanticScholarAdapter
from collector.application.dto import CollectionDraft
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.application.validation import ValidationError, validate_terms
from collector.domain.technology_mining import candidate_phrases, mine_technologies, targeted_since
from collector.domain.values import CollectionLimits, CollectionMode, OperationStatus, SearchTerms, SourceKey

from ..fakes import (
    FakeClock,
    FakeHttpClient,
    FakeRateLimiter,
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    ScriptedAdapter,
    raw_document,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
SINCE = date(2024, 10, 7)
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
EMPTY_FEED = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
ONE_ENTRY = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2602.17449v1</id><published>2026-02-19T10:00:00Z</published>
<updated>2026-02-19T10:00:00Z</updated><title>ACOS: Arrays of Cheap Optical Switches</title>
<summary>Optical circuit switching for AI training clusters.</summary>
<link href="http://arxiv.org/abs/2602.17449v1" rel="alternate" type="text/html"/></entry></feed>"""


async def drain(iterator) -> list:  # noqa: ANN001
    return [item async for item in iterator]


class MiningTest(unittest.TestCase):
    def test_candidates_skip_cyrillic_stopword_edges_and_frame_words(self) -> None:
        self.assertEqual(candidate_phrases("Новые методы защиты ИИ"), set())
        found = candidate_phrases("Optical circuit switching for AI clusters: a survey")
        self.assertIn("optical circuit switching", found)
        self.assertNotIn("switching for", found)
        self.assertNotIn("clusters a survey", found)

    def test_ranks_repeated_specific_phrases_and_drops_generic_common_and_topic(self) -> None:
        titles = [
            ("KV cache offloading for long-context inference", "arxiv"),
            ("Tiered KV cache offloading on CXL memory", "semantic_scholar"),
            ("LMCache: KV cache offloading engine", "github"),
            ("Large language models in production", "openalex"),
            ("Large language models for code", "arxiv"),
            ("Data storage systems for AI at scale", "openalex"),
            ("Data storage systems for AI clusters", "arxiv"),
        ] + [(f"Unrelated title number {i}", "rss") for i in range(5)]
        mined = mine_technologies(titles, exclude=("data storage systems for AI",), limit=5)
        self.assertEqual(mined[0], "kv cache offloading")
        self.assertNotIn("large language models", mined)          # общая область
        self.assertFalse(any("data storage" in item for item in mined))  # сама тема запроса
        self.assertNotIn("cache offloading", mined)               # вложенный вариант поглощён длинным

    def test_too_common_phrase_is_the_topic_itself(self) -> None:
        titles = [(f"Edge inference accelerators study {i}", "arxiv") for i in range(6)] + [("Other", "rss")] * 2
        self.assertNotIn("edge inference accelerators", mine_technologies(titles))

    def test_targeted_since_bounded_by_year(self) -> None:
        self.assertEqual(targeted_since(NOW, 24, 2023), date(2024, 10, 6))  # 24 × 30,44 дня = 731 день
        self.assertEqual(targeted_since(NOW, 60, 2023), date(2023, 1, 1))


class ValidationTest(unittest.TestCase):
    def test_technologies_are_validated(self) -> None:
        terms = validate_terms(("защита",), ("ai security",), ("  mcp   security scanners ", "x402"))
        self.assertEqual(terms.technologies, ("mcp security scanners", "x402"))
        with self.assertRaises(ValidationError):
            validate_terms(("a1",), (), ("x402", "X402"))
        with self.assertRaises(ValidationError):
            validate_terms(("a1",), (), tuple(f"tech {i}" for i in range(41)))


class TargetedAdaptersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_arxiv_gate()
        self.http = FakeHttpClient()
        self.clock = FakeClock(NOW)

    async def test_arxiv_phrase_in_date_window_by_relevance_then_all_words(self) -> None:
        self.http.enqueue(EMPTY_FEED)
        self.http.enqueue(ONE_ENTRY)
        adapter = ArxivAdapter(self.http, FakeRateLimiter(), self.clock)
        documents = await drain(adapter.search_targeted(["optical circuit switching"], 3, SINCE, 10_000.0))
        first, second = (call[1]["params"] for call in self.http.calls)
        self.assertEqual(first["sortBy"], "relevance")
        self.assertEqual(first["max_results"], 3)
        self.assertEqual(first["search_query"],
                         'all:"optical circuit switching" AND submittedDate:[202410070000 TO 202610072359]')
        self.assertTrue(second["search_query"].startswith("all:optical AND all:circuit AND all:switching"))
        self.assertEqual([d.matched_term for d in documents], ["optical circuit switching"])
        self.assertEqual(phrase_query("KV cache  offloading"), 'all:"KV cache offloading"')

    async def test_openalex_github_s2_hn_requests(self) -> None:
        self.http.enqueue({"results": [{"id": "https://openalex.org/W1", "title": "KV cache offloading",
                                         "publication_date": "2025-03-01", "doi": None}]})
        await drain(OpenAlexAdapter(self.http, FakeRateLimiter(), self.clock, api_key="k").search_targeted(
            ["kv cache offloading"], 2, SINCE, 10_000.0))
        params = self.http.calls[-1][1]["params"]
        self.assertEqual((params["search"], params["filter"], params["per-page"]),
                         ("kv cache offloading", "from_publication_date:2024-10-07", 2))
        technologies = [f"tech {i}" for i in range(MAX_TARGETED_TERMS + 5)]
        for _ in technologies:
            self.http.enqueue({"items": []})
        await drain(GithubAdapter(self.http, FakeRateLimiter(), self.clock).search_targeted(technologies, 3, SINCE, 10_000.0))
        github_calls = [c for c in self.http.calls if "api.github.com" in c[0]]
        self.assertEqual(len(github_calls), MAX_TARGETED_TERMS)
        self.assertEqual(github_calls[0][1]["params"], {"q": "tech 0 pushed:>=2024-10-07", "per_page": 3})
        self.http.enqueue({"data": []})
        await drain(SemanticScholarAdapter(self.http, FakeRateLimiter(), self.clock).search_targeted(
            ['x402 "payments"'], 3, SINCE, 10_000.0))
        url, call = self.http.calls[-1]
        self.assertEqual(url, RELEVANCE_URL)
        self.assertEqual((call["params"]["query"], call["params"]["limit"], call["params"]["publicationDateOrYear"]),
                         ("x402 payments", 3, "2024-10-07:"))
        self.http.enqueue({"hits": []})
        await drain(HackerNewsAdapter(self.http, FakeRateLimiter(), self.clock).search_targeted(["x402"], 3, SINCE, 10_000.0))
        params = self.http.calls[-1][1]["params"]
        start = int(datetime(2024, 10, 7, tzinfo=UTC).timestamp())
        self.assertEqual((params["query"], params["hitsPerPage"], params["numericFilters"]), ("x402", 3, f"created_at_i>{start}"))


class TargetedScriptedAdapter(ScriptedAdapter):
    supports_targeted = True

    def __init__(self, *args, targeted: dict[str, int] | None = None, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.targeted_calls: list[list[str]] = []
        self._targeted = targeted or {}

    async def search_targeted(self, technologies, per_term, since, deadline):  # noqa: ANN001, ANN201
        self.targeted_calls.append(list(technologies))
        for term in technologies:
            for i in range(min(per_term, self._targeted.get(term, 1))):
                yield raw_document(f"https://example.org/{term.replace(' ', '-')}/{i}", title=f"{term} paper {i}",
                                   matched_term=term)


class TwoPhaseCollectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.runs = InMemoryAdapterRunRepository()
        self.collections = InMemoryCollectionRepository(self.clock, self.runs)
        self.documents = InMemoryDocumentRepository(self.clock, self.collections)
        self.classification = load_classification_config(CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml")

    async def test_broad_then_targeted_with_model_and_mined_technologies(self) -> None:
        broad = [raw_document(f"https://example.org/broad/{i}", title=title)
                 for i, title in enumerate(["KV cache offloading at scale", "Tiered KV cache offloading on CXL",
                                            "Survey of storage", "KV cache offloading engine"]
                                           + [f"Unrelated broad paper {n}" for n in range(6)])]
        openalex = TargetedScriptedAdapter(SourceKey.OPENALEX, broad, targeted={"optical circuit switching": 2})
        rss = ScriptedAdapter(SourceKey.RSS, [raw_document("https://techcrunch.com/2026/10/01/a", title="Startup news")])
        collection, _ = await self.collections.create_if_absent(CollectionDraft(
            idempotency_key="job-1:collect", query_text="инфраструктура для ИИ", mode=CollectionMode.SEARCH,
            terms=SearchTerms(ru=("инфраструктура ИИ",), en=("ai infrastructure",), technologies=("optical circuit switching",)),
            limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=(SourceKey.OPENALEX, SourceKey.RSS)))
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        use_case = RunCollection(
            collections=self.collections, documents=self.documents, adapter_runs=self.runs,
            adapters={SourceKey.OPENALEX: openalex, SourceKey.RSS: rss}, classification=self.classification,
            clock=self.clock, config=RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01),
        )
        await use_case.execute(collection, "collector:test")
        self.assertEqual(self.collections.mined[collection.collection_id], ("kv cache offloading",))
        self.assertEqual(openalex.targeted_calls, [["optical circuit switching", "kv cache offloading"]])
        stored = {doc.url for doc in self.documents.documents.values()}
        self.assertIn("https://example.org/optical-circuit-switching/1", stored)
        self.assertIn("https://example.org/kv-cache-offloading/0", stored)
        self.assertIn("https://techcrunch.com/2026/10/01/a", stored)

    async def test_no_targeted_capable_source_keeps_single_pass(self) -> None:
        adapter = ScriptedAdapter(SourceKey.OPENALEX, [raw_document(f"https://example.org/{i}", title=f"Paper {i}",
                                                                    text=f"Abstract number {i}") for i in range(3)])
        collection, _ = await self.collections.create_if_absent(CollectionDraft(
            idempotency_key="job-2:collect", query_text="тема", mode=CollectionMode.SEARCH,
            terms=SearchTerms(ru=("тема",), en=("topic",), technologies=("x402",)),
            limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=(SourceKey.OPENALEX,)))
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        use_case = RunCollection(
            collections=self.collections, documents=self.documents, adapter_runs=self.runs,
            adapters={SourceKey.OPENALEX: adapter}, classification=self.classification, clock=self.clock,
            config=RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01),
        )
        await use_case.execute(collection, "collector:test")
        self.assertNotIn(collection.collection_id, self.collections.mined)
        self.assertEqual(len(self.documents.documents), 3)


if __name__ == "__main__":
    unittest.main()
