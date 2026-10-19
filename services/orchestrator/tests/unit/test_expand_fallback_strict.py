"""R-049 (C-3116): строгий резервный перевод запроса в стадии expand — флаг WS_QUERY_FALLBACK_STRICT."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.application.stages import expand
from orchestrator.application.stages.analyze import AnalyzeConfig
from orchestrator.application.stages.collect import CollectConfig
from orchestrator.application.stages.expand import Glossary, fallback_expansion, run_expand
from orchestrator.application.stages.narrate import NarrateConfig
from orchestrator.application.use_cases.run_job import RunJob, RunJobConfig, _bounded_expand
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.values import Decision

from ..fakes import (
    FakeAnalyzer,
    FakeClock,
    FakeCollector,
    FakeInsight,
    InMemoryJobRepository,
    InMemoryResultRepository,
    make_candidate,
    make_document,
    new_id,
)

GLOSSARY = Glossary.load(Path(__file__).resolve().parents[2] / "config" / "glossary_ru_en.yaml")
MEDIA = "медиа и развлечения"
# Шесть запросов кейса (tools/theme_analytics.py, THEMES) → переводы, которые должны быть в английской фразе.
CASE_QUERIES = {
    "индустриальный искусственный интеллект на производстве": ("industrial", "artificial intelligence",
                                                               "manufacturing"),
    "робототехника и физический искусственный интеллект": ("robotics", "physical", "artificial intelligence"),
    "инфраструктура для искусственного интеллекта и дата-центры": ("infrastructure", "artificial intelligence",
                                                                   "data center"),
    "перспективные решения в финтехе": ("fintech",),
    "защита и безопасность систем искусственного интеллекта": ("protection", "security", "systems",
                                                               "artificial intelligence"),
    "edge-вычисления и периферийный инференс": ("edge computing", "inference"),
}
# Прежний вывод на словаре orchestrator (замер X32): без флага он не меняется.
LEGACY_EN = {
    MEDIA: ("medicine",),
    "инфраструктура для искусственного интеллекта и дата-центры": ("artificial intelligence",),
    "защита и безопасность систем искусственного интеллекта": ("protection security artificial intelligence",
                                                               "security artificial intelligence"),
    "edge-вычисления и периферийный инференс": ("edge",),
}


class _SlowInsight:
    async def expand_query(self, query_text: str) -> None:
        await asyncio.sleep(5)


class _Recorder:
    """Журнал-дублёр: запоминает события и поля."""

    def __init__(self, events: list) -> None:
        self.events = events

    def __getattr__(self, level: str):  # noqa: ANN204
        return lambda event, **fields: self.events.append((level, event, fields))


class StrictFallbackExpansionTest(unittest.IsolatedAsyncioTestCase):
    """Флаг включён: резервные термины orchestrator без подмены и обеднения темы."""

    def test_media_is_not_medicine(self) -> None:
        expansion = fallback_expansion(MEDIA, GLOSSARY, strict=True)
        self.assertNotIn("medicine", " ".join(expansion.en_terms))
        self.assertIn("media entertainment", expansion.en_terms)

    def test_case_queries_keep_content_words(self) -> None:
        for query, expected in CASE_QUERIES.items():
            with self.subTest(query=query):
                phrase = fallback_expansion(query, GLOSSARY, strict=True).en_terms[0]
                for translation in expected:
                    self.assertIn(translation, phrase)

    def test_untranslated_words_are_logged(self) -> None:
        events: list = []
        with mock.patch.object(expand, "log", _Recorder(events)):
            fallback_expansion("квантовые голограммы", GLOSSARY, strict=True)
        self.assertIn(("warning", "stage.expand_untranslated", {"words": ["голограммы"]}), events)

    async def test_flag_reaches_fallback_on_insight_failure_and_timeout(self) -> None:
        for expansion in (
            await run_expand(FakeInsight(fail_expand=True), MEDIA, GLOSSARY, strict=True),
            await run_expand(None, MEDIA, GLOSSARY, strict=True),
            await _bounded_expand(_SlowInsight(), MEDIA, GLOSSARY, 0.05, strict=True),
        ):
            self.assertTrue(expansion.used_fallback)
            self.assertIn("media entertainment", expansion.en_terms)

    async def test_run_job_config_flag(self) -> None:
        strict = await _expand_trace(RunJobConfig(**_BASE, query_fallback_strict=True))
        default = await _expand_trace(RunJobConfig(**_BASE))
        self.assertIn("media entertainment", strict["en_terms"])
        self.assertEqual(default["en_terms"], ["medicine"])


class FlagOffUnchangedTest(unittest.IsolatedAsyncioTestCase):
    """Без флага — прежний вывод: дополнение словаря и строгие правила не действуют."""

    async def test_legacy_output(self) -> None:
        for query, expected in LEGACY_EN.items():
            with self.subTest(query=query):
                self.assertEqual(fallback_expansion(query, GLOSSARY).en_terms, expected)
                self.assertEqual((await run_expand(None, query, GLOSSARY)).en_terms, expected)

    def test_no_untranslated_log_without_flag(self) -> None:
        events: list = []
        with mock.patch.object(expand, "log", _Recorder(events)):
            fallback_expansion("квантовые голограммы", GLOSSARY)
        self.assertEqual(events, [])


_BASE = dict(lease_seconds=60, heartbeat_seconds=0, collect=CollectConfig(poll_seconds=1.0),
             analyze=AnalyzeConfig(poll_seconds=1.0), narrate=NarrateConfig(top_n=15))


async def _expand_trace(config: RunJobConfig) -> dict:
    """Задание с недоступным расширением insight; возвращает трассу стадии expand."""
    clock = FakeClock()
    jobs = InMemoryJobRepository(clock)
    results = InMemoryResultRepository(jobs)
    documents = {make_document(index).document_id: make_document(index) for index in (1, 2)}
    query = Query.create(new_id(2), MEDIA, 2)
    job = await jobs.insert(query, Job(job_id=new_id(1), query_id=query.query_id), "key-0001:submit", "hash",
                            clock.now())
    run_job = RunJob(
        jobs=jobs,
        results=results,
        collector=FakeCollector(statuses=["COMPLETED"], documents=documents),
        analyzer=FakeAnalyzer(statuses=["COMPLETED"], candidates=[
            make_candidate(1, rank=1, score=0.92), make_candidate(2, rank=2, score=0.81),
            make_candidate(3, decision=Decision.MATURE, score=0.15)]),
        insight=FakeInsight(fail_expand=True),
        glossary=GLOSSARY,
        clock=clock,
        config=config,
    )
    await run_job.execute(job, "worker-1")
    return results.traces[job.job_id]["expand"]


if __name__ == "__main__":
    unittest.main()
