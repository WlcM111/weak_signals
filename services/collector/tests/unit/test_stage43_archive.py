"""Этап 4.3: слова индексов, фильтры, оценка возникновения, Wayback, RSS-архив, корпус arXiv, новые адаптеры, отбор."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace

from collector.adapters.outbound.news_archive.arxiv_oai import ArxivOaiHarvester, parse_page
from collector.adapters.outbound.news_archive.rss_archive import archive_entries
from collector.adapters.outbound.news_archive.wayback import (
    Publisher,
    WaybackHarvester,
    article_entry,
    cdx_query,
    months_between,
    parse_cdx,
    published_from_path,
)
from collector.adapters.outbound.sources.crossref import CrossrefAdapter
from collector.adapters.outbound.sources.crossref import to_raw_document as crossref_doc
from collector.adapters.outbound.sources.huggingface import HuggingFaceAdapter
from collector.adapters.outbound.sources.huggingface import to_raw_document as hf_doc
from collector.adapters.outbound.sources.news_archive import NewsArchiveAdapter
from collector.application.ports import HttpStatusError
from collector.domain.archive import ArchiveEntry, CorpusHarvestState
from collector.domain.emergence import emerging_terms
from collector.domain.technology_filters import is_mature, is_verbish, mature_key
from collector.domain.technology_mining import candidate_phrases
from collector.domain.text_tokens import phrase_words, slug_title, tokens_of
from collector.domain.values import CollectionLimits, CollectionMode, SearchTerms

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter

LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class _Http:
    def __init__(self, responses: list[object]) -> None:
        self.responses, self.urls = list(responses), []

    async def get_text(self, url, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
        self.urls.append((url, dict(params or {})))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Archive:
    def __init__(self, entries: list[ArchiveEntry] | None = None, titles: list[tuple[str, date]] | None = None) -> None:
        self.saved: list[ArchiveEntry] = []
        self.marks: list[tuple[str, str, str, int]] = []
        self.done: dict[str, dict[str, str]] = {}
        self.entries, self.titles, self.searches = entries or [], titles or [], []

    async def save_entries(self, entries):  # noqa: ANN001, ANN201
        self.saved.extend(entries)
        return len(entries)

    async def search(self, words, since, limit, min_match):  # noqa: ANN001, ANN201
        self.searches.append((tuple(words), since, limit, min_match))
        ranked = sorted(((len(set(words) & set(e.tokens)), e) for e in self.entries), key=lambda item: -item[0])
        return [e for matched, e in ranked if matched >= min_match][:limit]

    async def titles_matching(self, keywords, min_overlap, start, end, limit):  # noqa: ANN001, ANN201
        return [(title, day) for title, day in self.titles if start <= day < end][:limit]

    async def harvested_periods(self, publisher):  # noqa: ANN001, ANN201
        return self.done.get(publisher, {})

    async def mark_harvest(self, publisher, period, status, rows, detail):  # noqa: ANN001, ANN201
        self.marks.append((publisher, period, status, rows))


class TokensAndFiltersTest(unittest.TestCase):
    def test_tokens_phrases_and_slug_titles(self) -> None:
        self.assertEqual(tokens_of("Ёжик и KV-cache для AI"), ["ежик", "kv", "cache", "ai"])
        self.assertEqual(phrase_words("AI agent payment authorization protocols for banks"),
                         ["ai", "agent", "payment", "authorization", "protocols"])
        self.assertEqual(slug_title("/2026/02/02/openmind-launches-app-store-for-robots/98554/"),
                         "openmind launches app store for robots")

    def test_fragments_verbs_generic_and_mature(self) -> None:
        found = candidate_phrases("Deep reinforcement learning for code owns the framework integrating models")
        self.assertFalse(any(p.startswith("deep reinforcement") or "owns" in p or "integrating" in p for p in found))
        self.assertEqual(candidate_phrases("Future directions of social media"), set())
        self.assertTrue(is_verbish(["addressing", "regulatory"]))
        self.assertFalse(is_verbish(["kv", "cache", "offloading"]))
        self.assertEqual(mature_key("Federated learning for distributed factories"), "federated learning")
        self.assertTrue(is_mature("anomaly detection on IoT data streams"))
        self.assertFalse(is_mature("optical circuit switching"))


class EmergenceTest(unittest.TestCase):
    def test_growing_specific_phrase_wins_and_topic_is_excluded(self) -> None:
        rows = [(f"Optical circuit switching study {i}", date(2025, 6, 1)) for i in range(3)]
        rows += [(f"Packet switching networks survey {i}", date(2022, 3, 1)) for i in range(3)]
        rows += [("Packet switching networks today", date(2025, 3, 1))]
        rows += [(f"Data center fabric topic {i}", date(2025, 4, 1)) for i in range(2)]
        rows += [(f"Filler paper {i}", date(2022, 1, 1)) for i in range(10)] + [(f"Other {i}", date(2025, 7, 1)) for i in range(15)]
        result = emerging_terms(rows, recent_from=date(2024, 10, 10), base_from=date(2021, 1, 1),
                                base_to=date(2024, 10, 9), exclude=("data center fabric",), min_recent=2)
        terms = [item.term for item in result]
        self.assertEqual(terms[0], "optical circuit switching")
        self.assertNotIn("packet switching networks", terms)  # нет роста
        self.assertFalse(any("data center" in term for term in terms))  # сама тема


class WaybackTest(unittest.IsolatedAsyncioTestCase):
    def test_queries_parsing_and_entries(self) -> None:
        dated = Publisher("techcrunch.com", "url_month", ("techcrunch.com/{yyyy}/{mm}/",))
        section = Publisher("prnewswire.com", "capture_month", ("prnewswire.com/news-releases/",), tech_filter=True)
        q1, q2 = cdx_query(dated.prefixes[0], "2026-09", dated, None), cdx_query(section.prefixes[0], "2026-09", section, "k1")
        self.assertIn("url=techcrunch.com%2F2026%2F09%2F", q1)
        self.assertNotIn("from=", q1)
        self.assertIn("from=202609", q2)
        self.assertIn("resumeKey=k1", q2)
        self.assertEqual(q2.count("filter="), 3)
        self.assertEqual(months_between("2025-11", "2026-02"), ["2025-11", "2025-12", "2026-01", "2026-02"])
        body = json.dumps([["original", "timestamp"], ["https://techcrunch.com/2026/09/04/a-b-c/", "20260905"], [], ["next"]])
        self.assertEqual(parse_cdx(body), ([("https://techcrunch.com/2026/09/04/a-b-c/", "20260905")], "next"))
        entry = article_entry("https://techcrunch.com/2026/09/04/robot-skill-marketplace-raises-seed/?utm=x", "20260906101010", "techcrunch.com")
        self.assertEqual((entry.url, entry.published_on, entry.title), (
            "https://techcrunch.com/2026/09/04/robot-skill-marketplace-raises-seed/", date(2026, 9, 4),
            "Robot skill marketplace raises seed"))
        self.assertIsNone(article_entry("https://techcrunch.com/tag/robotics-startups-news/", "2026", "techcrunch.com"))
        self.assertIsNone(article_entry("https://techcrunch.com/2026/09/04/short/", "20260906", "techcrunch.com"))
        self.assertEqual(published_from_path("/business/spains-ipronics-raises-125-million-2026-09-02/"), date(2026, 9, 2))
        self.assertEqual(published_from_path("/news/home/20260316829602/en/Edge-Platform"), date(2026, 3, 16))
        undated = article_entry("https://pulse2.com/keycard-launched-with-38-million/", "20260115000000", "pulse2.com")
        self.assertEqual(undated.published_on, date(2026, 1, 15))  # дата сохранения в архиве

    async def test_harvest_skips_done_refreshes_last_and_marks_failures(self) -> None:
        publisher = Publisher("techcrunch.com", "url_month", ("techcrunch.com/{yyyy}/{mm}/",))
        page = json.dumps([["original", "timestamp"], ["https://techcrunch.com/2026/09/04/robot-skill-marketplace-raises-seed/", "20260905"]])
        store = _Archive()
        store.done = {"techcrunch.com": {"2026-08": "done", "2026-09": "done"}}
        http = _Http([page, HttpStatusError(404, "нет")])
        months = ["2026-07", "2026-08", "2026-09"]
        await WaybackHarvester(http, store, FakeClock(NOW), 0.0, report=lambda _line: None).harvest([publisher], months, ["2026-09"])
        self.assertEqual([m[1:] for m in store.marks], [("2026-07", "done", 1), ("2026-09", "failed", 0)])  # 2026-08 пропущен
        self.assertEqual(len(store.saved), 1)


class RssAndArxivTest(unittest.IsolatedAsyncioTestCase):
    def test_rss_entries_keep_every_record(self) -> None:
        entries = archive_entries([
            SimpleNamespace(title="Startup bets on solid-state transformers", link="https://www.example.com/a/b?x=1",
                            summary="<p>Pilot &amp; funding</p>", published_at=datetime(2026, 10, 1, tzinfo=UTC)),
            SimpleNamespace(title="", link="https://example.com/c", summary="", published_at=None),
        ], NOW)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].publisher, entries[0].url, entries[0].summary, entries[0].published_on),
                         ("example.com", "https://www.example.com/a/b", "Pilot & funding", date(2026, 10, 1)))

    def test_oai_page_filters_years_and_reads_token(self) -> None:
        body = """<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>
<record><header><identifier>oai:arXiv.org:2501.00001</identifier></header><metadata>
<arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>2501.00001</id><created>2025-01-02</created>
<title>Optical circuit switching for training clusters</title><categories>cs.NI cs.DC</categories>
<abstract>We study OCS.</abstract></arXiv></metadata></record>
<record><header><identifier>oai:arXiv.org:1901.00001</identifier></header><metadata>
<arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>1901.00001</id><created>2019-01-02</created><title>Old work</title>
<categories>cs.AI</categories><abstract>x</abstract></arXiv></metadata></record>
<record><header status="deleted"><identifier>oai:arXiv.org:2502.1</identifier></header></record>
<resumptionToken cursor="0">tok-2</resumptionToken></ListRecords></OAI-PMH>"""
        papers, seen, token, empty = parse_page(body, 2021)
        self.assertEqual(([p.paper_id for p in papers], seen, token, empty), (["2501.00001"], 3, "tok-2", False))
        self.assertEqual(papers[0].categories, ("cs.NI", "cs.DC"))

    async def test_harvest_resumes_from_saved_token(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.state = CorpusHarvestState("cs", "tok-1", 1000, 10, "failed")
                self.papers: list[object] = []

            async def harvest_state(self, set_spec):  # noqa: ANN001, ANN201
                return self.state

            async def save_harvest_state(self, state, detail=""):  # noqa: ANN001, ANN201
                self.state = state

            async def save_papers(self, papers):  # noqa: ANN001, ANN201
                self.papers.extend(papers)
                return len(papers)

        last = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><resumptionToken/></ListRecords></OAI-PMH>'
        http, store = _Http([last]), Store()
        state = await ArxivOaiHarvester(http, store, FakeClock(NOW), 0.0, report=lambda _l: None).harvest("cs")
        self.assertEqual(http.urls[0][1], {"verb": "ListRecords", "resumptionToken": "tok-1"})
        self.assertEqual((state.status, state.records, state.resumption_token), ("done", 1000, None))


class NewAdaptersTest(unittest.IsolatedAsyncioTestCase):
    async def test_news_archive_broad_and_targeted(self) -> None:
        entry = ArchiveEntry("https://techcrunch.com/2026/09/04/robot-skill-marketplace/", "techcrunch.com", "wayback",
                             "Robot skill marketplace", date(2026, 9, 4), tokens=("robot", "skill", "marketplace"))
        store = _Archive(entries=[entry])
        adapter = NewsArchiveAdapter(FakeHttpClient(), FakeRateLimiter(), FakeClock(NOW), store)
        terms = SearchTerms(en=("robot software",), technologies=("robot skill marketplace",))
        docs = [d async for d in adapter.search(terms, LIMITS, 10_000.0)]
        self.assertEqual([d.matched_term for d in docs], ["robot skill marketplace"])
        self.assertEqual(store.searches[0][0][:2], ("robot", "robots"))  # этап 4.4: сначала технология, обе формы числа
        self.assertEqual(store.searches[0][3], 2)  # из трёх слов фразы должны совпасть два
        targeted = [d async for d in adapter.search_targeted(["skill marketplace"], 5, date(2024, 10, 1), 10_000.0)]
        self.assertEqual((targeted[0].url, targeted[0].raw_meta), (entry.url, {"publisher": "techcrunch.com", "origin": "wayback"}))
        self.assertEqual(store.searches[-1], (("skill", "skills", "marketplace", "marketplaces"), date(2024, 10, 1), 5, 2))

    async def test_crossref_and_huggingface_requests_and_parsing(self) -> None:
        http = FakeHttpClient()
        http.enqueue({"message": {"items": [{"DOI": "10.1/x", "title": ["Optical switching"], "issued": {"date-parts": [[2025, 3]]},
                                             "abstract": "<jats:p>Fabric</jats:p>", "is-referenced-by-count": 4}]}})
        docs = [d async for d in CrossrefAdapter(http, FakeRateLimiter(), FakeClock(NOW), "ops@example.org").search_targeted(
            ["optical switching"], 2, date(2024, 10, 1), 10_000.0)]
        params = http.calls[-1][1]["params"]
        self.assertEqual((params["query.bibliographic"], params["filter"], params["rows"], params["mailto"]),
                         ("optical switching", "from-pub-date:2024-10-01", 2, "ops@example.org"))
        self.assertEqual((docs[0].url, docs[0].doi, docs[0].citation_count, docs[0].text), (
            "https://doi.org/10.1/x", "10.1/x", 4, "Optical switching Fabric"))
        self.assertIsNone(crossref_doc({"title": []}, "x"))
        http.enqueue([{"id": "org/kv-cache-offload", "likes": 7, "downloads": 3, "createdAt": "2025-05-01T00:00:00.000Z",
                       "tags": ["llm", "region:us"]}])
        docs = [d async for d in HuggingFaceAdapter(http, FakeRateLimiter(), FakeClock(NOW)).search_targeted(
            ["kv cache"], 3, date(2024, 10, 1), 10_000.0)]
        self.assertEqual(http.calls[-1][1]["params"]["search"], "kv cache")
        self.assertEqual((docs[0].url, docs[0].engagement_count, docs[0].text), (
            "https://huggingface.co/org/kv-cache-offload", 7, "kv cache offload llm"))
        self.assertIsNone(hf_doc({"id": "noslash"}, "x", "model"))


if __name__ == "__main__":
    unittest.main()


class SelectionOrderTest(unittest.IsolatedAsyncioTestCase):
    """Этап 4.3: технологии из корпусов идут первыми, технологии модели — вспомогательно, не больше llm_technologies."""

    async def test_emerging_first_then_mined_then_capped_model(self) -> None:
        from pathlib import Path

        from collector.adapters.outbound.rules_loader import load_classification_config
        from collector.application.dto import CollectionDraft
        from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
        from collector.domain.values import OperationStatus, SourceKey

        from ..fakes import InMemoryAdapterRunRepository, InMemoryCollectionRepository, InMemoryDocumentRepository, raw_document
        from .test_targeted_search import TargetedScriptedAdapter

        class Corpus:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            async def titles_matching(self, keywords, min_overlap, start, end, limit):  # noqa: ANN001, ANN201
                self.calls.append((tuple(keywords), min_overlap, start, end, limit))
                rows = [(f"Optical circuit switching study {i}", date(2026, 3, 1)) for i in range(4)]
                rows += [(f"Background paper {i}", date(2022, 2, 1)) for i in range(12)]
                rows += [(f"Recent other {i}", date(2026, 1, 1)) for i in range(30)]  # доля фразы < 15 %
                return [(title, day) for title, day in rows if start <= day < end][:limit]

        clock = FakeClock()
        runs = InMemoryAdapterRunRepository()
        collections = InMemoryCollectionRepository(clock, runs)
        documents = InMemoryDocumentRepository(clock, collections)
        config_dir = Path(__file__).resolve().parents[2] / "config"
        adapter = TargetedScriptedAdapter(SourceKey.OPENALEX, [raw_document(f"https://example.org/{i}", title=f"Paper {i}")
                                                               for i in range(3)])
        model = tuple(f"model technology {i}" for i in range(6)) + ("federated learning for factories",)
        collection, _ = await collections.create_if_absent(CollectionDraft(
            idempotency_key="job-43:collect", query_text="инфраструктура ИИ", mode=CollectionMode.SEARCH,
            terms=SearchTerms(ru=("инфраструктура ИИ",), en=("ai data center networking",), technologies=model),
            limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=(SourceKey.OPENALEX,)))
        collection.transition_to(OperationStatus.RUNNING, clock.now())
        use_case = RunCollection(
            collections=collections, documents=documents, adapter_runs=runs, adapters={SourceKey.OPENALEX: adapter},
            classification=load_classification_config(config_dir / "trust_rules.yaml", config_dir / "rss_domains.yaml"),
            clock=clock, config=RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01,
                                                    llm_technologies=2),
            corpus=(corpus := Corpus()),
        )
        await use_case.execute(collection, "collector:test")
        # этап 4.4: ключевые слова темы без общих («ai»), хотя бы 2 совпадения, два периода — недавний и базовый
        self.assertEqual(corpus.calls[0][0][:4], ("center", "centers", "networking", "networkings"))
        self.assertEqual([call[1] for call in corpus.calls], [2, 2])
        self.assertEqual(corpus.calls[1][3], corpus.calls[0][2])  # базовый период кончается там, где начинается недавний
        first = adapter.targeted_calls[0]
        self.assertEqual(first[0], "optical circuit switching")
        self.assertEqual(collections.emerging[collection.collection_id][0], "optical circuit switching")
        self.assertEqual([t for t in first if t.startswith("model technology")], ["model technology 0", "model technology 1"])
        self.assertNotIn("federated learning for factories", first)


class Stage44BudgetTest(unittest.TestCase):
    """Этап 4.4: лимиты широкого прохода новых источников, ключевые слова темы, формы числа."""

    def test_broad_caps_and_per_term_overrides(self) -> None:
        from collector.application.use_cases.run_collection import RunCollectionConfig, broad_cap
        from collector.domain.values import SourceKey

        config = RunCollectionConfig()
        self.assertEqual(config.targeted_reserve_documents, 600)
        self.assertEqual(broad_cap(config, LIMITS, SourceKey.CROSSREF), 20)
        self.assertEqual(broad_cap(config, LIMITS, SourceKey.NEWS_ARCHIVE), 20)
        self.assertEqual(broad_cap(config, LIMITS, SourceKey.HUGGINGFACE), 10)
        self.assertEqual(broad_cap(config, LIMITS, SourceKey.OPENALEX), 60)
        self.assertEqual(config.targeted_per_term_overrides, {SourceKey.NEWS_ARCHIVE: 5, SourceKey.CROSSREF: 1})
        self.assertEqual(config.snowball_reserve_documents, 200)

    def test_topic_keywords_drop_generic_words(self) -> None:
        from collector.domain.technology_filters import topic_keywords, with_variants

        self.assertEqual(topic_keywords(["high-performance AI computing systems", "hybrid intelligent robots"]),
                         ["computing", "hybrid", "robots"])
        self.assertEqual(with_variants(["robots", "edge"]), ["robots", "robot", "edge", "edges"])
        self.assertEqual(topic_keywords(["искусственный интеллект в финтехе"]), ["финтехе"])

    def test_generic_phrases_from_measurement_are_rejected(self) -> None:
        found = candidate_phrases("Use cases and current state of public sector data collection: ieee transactions")
        self.assertFalse(found & {"use cases", "current state", "public sector", "data collection", "ieee transactions"})
