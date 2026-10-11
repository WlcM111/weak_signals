"""Этап 4.7: мусорные технологии, якоря темы, суточный лимит, время и лимит сбора, обогащение статей из сборов."""

from __future__ import annotations

import importlib.util
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from collector.adapters.outbound.news_archive.enrich import ArchiveEnricher
from collector.adapters.outbound.sources.base import LONG_BLOCK_SECONDS, daily_limit_exhausted
from collector.adapters.outbound.sources.hackernews import HackerNewsAdapter
from collector.application.ports import HttpStatusError
from collector.application.use_cases.run_collection import PATENT_SOURCES
from collector.domain.archive import ArchiveEntry
from collector.domain.errors import AdapterFailure
from collector.domain.event_channel import extract_entities, is_all_caps
from collector.domain.technology_filters import stem, topic_anchors
from collector.domain.technology_mining import candidate_phrases, mine_technologies
from collector.domain.values import AdapterErrorCode, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter

ROBOTICS = ["cognitive robotics", "AI-integrated materials", "adaptive robotic systems", "hybrid intelligent robots",
            "integration of AI and mechanics", "self-learning physical AI agents", "autonomous decision-making robots",
            "physical artificial intelligence in robotics"]


class JunkTechnologiesTest(unittest.TestCase):
    def test_no_bare_acronyms_generic_acronym_grams_or_patent_templates(self) -> None:
        found = candidate_phrases("LLM agents and VLM RAG pipelines for GPU HPC clusters")
        self.assertFalse({"llm", "vlm", "rag", "gpu", "hpc"} & found)  # одиночных аббревиатур нет
        self.assertNotIn("vlm rag", found)  # словосочетание только из общих аббревиатур
        self.assertIn("gpu hpc clusters", found)  # с конкретным словом — допустимо
        self.assertEqual(candidate_phrases("SYSTEM AND METHOD FOR SECURE PROCESSING"), {"secure processing"})
        self.assertEqual(candidate_phrases("Программно-аппаратный комплекс управления параметрами доверия"), set())
        # шаблон патента отсекается; заголовки патентов вообще не попадают в извлечение (PATENT_SOURCES)
        self.assertEqual(candidate_phrases("Способ предсказания межфазного натяжения"), {"межфазного натяжения"})

    def test_patent_titles_never_feed_mining_and_caps_titles_give_no_entities(self) -> None:
        self.assertEqual(PATENT_SOURCES, frozenset({SourceKey.ROSPATENT, SourceKey.PATENTSVIEW}))
        self.assertTrue(is_all_caps("SYSTEM AND METHOD FOR MANAGING TRUST PARAMETERS"))
        self.assertEqual(extract_entities(["SYSTEM RAISES PARAMETERS OF TRUST", "Keycard raises $38M"]), ["Keycard"])
        titles = [("LLM serving stack", "a.com"), ("LLM serving cluster", "b.com")] + [(f"Other {i}", f"s{i}.com") for i in range(10)]
        self.assertNotIn("llm", mine_technologies(titles))


class AnchorsTest(unittest.TestCase):
    def test_robotics_anchor_is_robot_family(self) -> None:
        self.assertEqual({stem(w) for w in ("robotics", "robotic", "robots", "robot")}, {"robot"})
        anchors = topic_anchors(ROBOTICS)
        self.assertTrue({"robotics", "robotic", "robots", "robot"} <= set(anchors))
        self.assertNotIn("agents", anchors)  # «agents» — в одной фразе темы: не якорь
        self.assertEqual(topic_anchors(["AI model security", "adversarial attacks on AI"]), [])  # якорей нет


class DailyLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_daily_429_fails_fast_and_blocks_later_requests(self) -> None:
        exhausted = HttpStatusError(429, "лимит", 9791.0, None, {"x-ratelimit-remaining-usd": "0"})
        self.assertTrue(daily_limit_exhausted(exhausted))
        self.assertFalse(daily_limit_exhausted(HttpStatusError(429, "частота", 30.0)))
        http, limiter = FakeHttpClient(), FakeRateLimiter()
        http.enqueue(exhausted)
        adapter = HackerNewsAdapter(http, limiter, FakeClock())
        with self.assertRaises(AdapterFailure) as ctx:
            await adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "x"})
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), (AdapterErrorCode.RATE_LIMITED.value, False))
        self.assertEqual(limiter.penalties[-1], (SourceKey.HACKERNEWS, 9791.0))
        limiter.blocked[SourceKey.HACKERNEWS] = 9000.0
        with self.assertRaises(AdapterFailure):
            await adapter.fetch_json("https://hn.algolia.com/api/v1/search", params={"query": "y"})
        self.assertEqual(adapter.http_requests, 1)  # второй вызов отказал без запроса и без ожидания
        self.assertGreaterEqual(9000.0, LONG_BLOCK_SECONDS)


class LimitsAndEnrichTest(unittest.IsolatedAsyncioTestCase):
    def test_quick_check_limits_come_from_environment(self) -> None:
        root = Path(__file__).resolve().parents[4]
        spec = importlib.util.spec_from_file_location("strategy_check", root / "tools" / "benchmark" / "strategy_check.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        with mock.patch.dict(os.environ, {"WS_COLLECT_TIME_BUDGET_SECONDS": "480", "WS_COLLECT_MAX_TOTAL_DOCUMENTS": "1450"}):
            self.assertEqual(module.collection_limits(), {"time_budget_seconds": 480, "max_total_documents": 1450})

    async def test_enrich_from_collections_uses_collected_entries(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls, self.saved = [], []

            async def entries_from_collections(self, limit):  # noqa: ANN001, ANN201
                self.calls.append(("collections", limit))
                return [ArchiveEntry("https://techcrunch.com/2026/08/11/x/", "techcrunch.com", "wayback", "X", date(2026, 8, 11))]

            async def entries_to_enrich(self, cues, limit):  # noqa: ANN001, ANN201
                self.calls.append(("archive", limit))
                return []

            async def save_enrichment(self, url, title, summary, published_on, text_status, tokens):  # noqa: ANN001, ANN201
                self.saved.append((url, text_status))

        class Http:
            async def get_text(self, url, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
                return '<html><head><meta name="description" content="Autonomous welding cells raise seed."/></head></html>'

        store = Store()
        done, failed = await ArchiveEnricher(Http(), store, FakeClock(), ("raises",), 0.0, report=lambda _l: None).run(
            10, from_collections=True)
        self.assertEqual((store.calls, done, failed), ([("collections", 10)], 1, 0))
        self.assertEqual(store.saved, [("https://techcrunch.com/2026/08/11/x/", "page")])
