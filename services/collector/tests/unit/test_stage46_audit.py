"""Этап 4.6: проверки по аудиту — P11 (CDX partial), P12 (инкремент OAI), P14 (квоты), P16 (независимость),
P18 (рост), P20/P22 (события и сущности), P08 (текст страницы), реестр лент."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from collector.adapters.outbound.news_archive.arxiv_oai import ArxivOaiHarvester
from collector.adapters.outbound.news_archive.enrich import extract_article
from collector.adapters.outbound.news_archive.feeds import feed_links, load_registry
from collector.adapters.outbound.news_archive.wayback import Publisher, WaybackHarvester
from collector.application.use_cases.run_collection import independence_key, select_by_quota
from collector.domain.archive import CorpusHarvestState
from collector.domain.emergence import emerging_terms
from collector.domain.event_channel import EVENT_CUES, extract_entities
from collector.domain.technology_mining import mine_technologies
from collector.domain.values import SourceKey

from ..fakes import FakeClock

CONFIG = Path(__file__).resolve().parents[2] / "config"


class _Http:
    def __init__(self, responses: list[str]) -> None:
        self.responses, self.calls = list(responses), []

    async def get_text(self, url, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


class _Store:
    def __init__(self) -> None:
        self.marks, self.saved, self.states = [], 0, {}

    async def harvest_states(self, publisher):  # noqa: ANN001, ANN201
        return self.states

    async def mark_harvest(self, publisher, period, status, rows, detail, resume=None):  # noqa: ANN001, ANN201
        self.marks.append((period, status, resume))

    async def save_entries(self, entries):  # noqa: ANN001, ANN201
        self.saved += len(entries)
        return len(entries)


def _page(key: str | None) -> str:
    rows = [["original", "timestamp"], ["https://techcrunch.com/2026/09/04/robot-skill-marketplace-raises-seed/", "20260905"]]
    return json.dumps(rows + ([[], [key]] if key else []))


class CdxPartialTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_limit_marks_partial_and_resumes(self) -> None:
        publisher = Publisher("techcrunch.com", "url_month", ("techcrunch.com/{yyyy}/{mm}/",))
        store = _Store()
        http = _Http([_page(f"k{i}") for i in range(1, 5)])
        await WaybackHarvester(http, store, FakeClock(), 0.0, report=lambda _l: None).harvest([publisher], ["2026-09"])
        self.assertEqual(store.marks[-1], ("2026-09", "partial", "0|k4"))  # после 4 страниц продолжение осталось
        store.states = {"2026-09": ("partial", "0|k4")}
        http = _Http([_page(None)])
        await WaybackHarvester(http, store, FakeClock(), 0.0, report=lambda _l: None).harvest([publisher], ["2026-09"])
        self.assertIn("resumeKey=k4", http.calls[0][0])
        self.assertEqual(store.marks[-1], ("2026-09", "done", None))


class OaiIncrementalTest(unittest.IsolatedAsyncioTestCase):
    async def test_done_set_updates_from_watermark(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.state = CorpusHarvestState("cs", None, 1000, 400, "done", date(2026, 9, 1))

            async def harvest_state(self, set_spec):  # noqa: ANN001, ANN201
                return self.state

            async def save_harvest_state(self, state, detail=""):  # noqa: ANN001, ANN201
                self.state = state

            async def save_papers(self, papers):  # noqa: ANN001, ANN201
                return len(papers)

        page = """<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><record><header>
<identifier>oai:arXiv.org:2610.00001</identifier><datestamp>2026-10-05</datestamp></header><metadata>
<arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>2610.00001</id><created>2026-10-04</created><title>New work</title>
<categories>cs.AI</categories><abstract>x</abstract></arXiv></metadata></record><resumptionToken/></ListRecords></OAI-PMH>"""
        http, store = _Http([page]), Store()
        state = await ArxivOaiHarvester(http, store, FakeClock(), 0.0, report=lambda _l: None).harvest("cs")
        self.assertEqual(http.calls[0][1]["from"], "2026-09-01")  # прежде — 0 запросов у завершённого набора
        self.assertEqual((state.status, state.watermark, state.kept), ("done", date(2026, 10, 5), 401))


class SelectionAndMiningTest(unittest.TestCase):
    def test_quotas_keep_every_channel(self) -> None:
        emerging = [f"emerging tech {i}" for i in range(25)]
        mined = [f"mined tech {i}" for i in range(15)]
        model = [f"model tech {i}" for i in range(10)]
        chosen, report = select_by_quota([("emerging", emerging, 15), ("model", model, 10), ("mined", mined, 9),
                                          ("entities", [], 6)], 40)
        self.assertEqual(len(chosen), 40)
        self.assertEqual(report["taken"]["model"], 10)  # аудит: прежде 0 из модели при 25+15+10
        self.assertGreaterEqual(report["taken"]["mined"], 9)

    def test_independence_by_publisher_not_adapter(self) -> None:
        self.assertEqual(independence_key(SourceKey.RSS, "https://www.techcrunch.com/a"), "techcrunch.com")
        self.assertEqual(independence_key(SourceKey.ARXIV, "https://arxiv.org/abs/1"), "https://arxiv.org/abs/1")
        titles = [("Optical circuit switching fabric", "techcrunch.com"), ("Optical circuit switching ramps", "siliconangle.com")]
        titles += [(f"Neutral title {i}", f"site{i}.com") for i in range(12)]
        self.assertIn("optical circuit switching", mine_technologies(titles))  # два издателя через один адаптер
        same = [("Optical circuit switching fabric", "techcrunch.com"), ("Optical circuit switching fabric", "theverge.com")]
        same += [(f"Neutral title {i}", f"site{i}.com") for i in range(12)]
        self.assertEqual(mine_technologies(same), [])  # перепечатка одного заголовка — одно свидетельство

    def test_growth_rules(self) -> None:
        base = [(f"Falling term paper {i}", date(2022, 1, 1)) for i in range(10)] + [(f"Base {i}", date(2022, 2, 1)) for i in range(90)]
        recent = [(f"Falling term paper {i}", date(2026, 1, 1)) for i in range(3)] + [(f"Recent {i}", date(2026, 2, 1)) for i in range(97)]
        terms = [t.term for t in emerging_terms([*base, *recent], recent_from=date(2025, 1, 1), base_from=date(2021, 1, 1),
                                                base_to=date(2024, 12, 31), min_recent=2)]
        self.assertNotIn("falling term paper", terms)  # 10 → 3: не рост
        self.assertEqual(emerging_terms(recent, recent_from=date(2025, 1, 1), base_from=date(2021, 1, 1),
                                        base_to=date(2024, 12, 31), min_recent=2, min_base_total=50), [])  # базы нет


class EventsEntitiesTest(unittest.TestCase):
    def test_multilingual_event_types_and_entities(self) -> None:
        for cue in ("pilot", "deployment", "patent", "привлекла", "внедрение"):
            self.assertIn(cue, EVENT_CUES)
        entities = extract_entities(["Keycard raises $38M for agent identity", "Tetragen Robotics launches welding cell",
                                     "Keycard raises again", "The startup raises money"])
        self.assertEqual(entities[:2], ["Keycard", "Tetragen Robotics"])
        self.assertNotIn("The", entities)


class PageAndRegistryTest(unittest.TestCase):
    def test_article_text_date_and_feed_links(self) -> None:
        page = """<html><head><meta property="og:title" content="Tetragen raises seed"/>
<meta name="description" content="Autonomous welding cells for shipyards."/>
<meta property="article:published_time" content="2026-08-11T09:00:00Z"/></head><body>
<p>Tetragen Robotics raised CAD 1.8 million to expand autonomous welding systems for heavy industry clients.</p>
<link rel="alternate" type="application/rss+xml" href="/feed/"/></body></html>"""
        title, summary, published = extract_article(page)
        self.assertEqual((title, published), ("Tetragen raises seed", date(2026, 8, 11)))
        self.assertIn("autonomous welding systems", summary)
        self.assertEqual(feed_links(page, "https://example.com/news"), ["https://example.com/feed/"])

    def test_registry_from_sources_document(self) -> None:
        sources = load_registry(CONFIG / "source_registry.yaml")
        ids = {s.source_id for s in sources}
        self.assertNotIn("R001", ids)  # Common Crawl не подключается
        self.assertIn("R047", ids)  # Open Compute Project
        self.assertGreater(len(sources), 200)
