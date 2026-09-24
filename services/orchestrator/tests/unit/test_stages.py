"""Стадии задания на дублёрах: расширение, сбор, анализ, нарратив, финализация."""

from __future__ import annotations

import unittest

from orchestrator.application.stages.analyze import AnalyzeConfig, run_analyze
from orchestrator.application.stages.collect import CollectConfig, run_collect
from orchestrator.application.stages.expand import Glossary, fallback_expansion, run_expand
from orchestrator.application.stages.finalize import build_stats, decide_final_status
from orchestrator.application.stages.narrate import (
    NarrateConfig,
    build_features,
    build_sources,
    run_narrate,
    truncate_at_sentence,
)
from orchestrator.domain.errors import StageFailed
from orchestrator.domain.values import Decision, JobStatus, NarrativeStatus, SummaryKind

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

GLOSSARY = Glossary({"технологии": "technologies", "искусственный интеллект": "artificial intelligence"})
JOB_ID = new_id(1)


async def no_check() -> None:
    """Проверка отмены, которая ничего не делает."""


class ExpandStageTest(unittest.IsolatedAsyncioTestCase):
    """Расширение запроса и его резервный вариант."""

    async def test_uses_insight_terms(self) -> None:
        expansion = await run_expand(FakeInsight(), "слабые сигналы", GLOSSARY)
        self.assertFalse(expansion.used_fallback)
        self.assertIn("weak signals", expansion.en_terms)

    async def test_fallback_on_insight_failure(self) -> None:
        expansion = await run_expand(FakeInsight(fail_expand=True), "технологии", GLOSSARY)
        self.assertTrue(expansion.used_fallback)
        self.assertEqual(expansion.ru_terms, ("технологии",))
        self.assertEqual(expansion.en_terms, ("technologies",))

    async def test_fallback_without_insight_service(self) -> None:
        expansion = await run_expand(None, "искусственный интеллект", GLOSSARY)
        self.assertTrue(expansion.used_fallback)
        self.assertEqual(expansion.en_terms, ("artificial intelligence",))

    def test_glossary_translates_word_by_word(self) -> None:
        self.assertEqual(GLOSSARY.translate("технологии"), "technologies")
        self.assertEqual(GLOSSARY.translate("неизвестное слово"), "неизвестное слово")

    def test_fallback_keeps_query_when_no_glossary(self) -> None:
        expansion = fallback_expansion("нейроморфные чипы", Glossary({}))
        self.assertEqual(expansion.ru_terms, ("нейроморфные чипы",))
        self.assertEqual(expansion.en_terms, ("нейроморфные чипы",))


class CollectStageTest(unittest.IsolatedAsyncioTestCase):
    """Сбор документов: опрос до терминального статуса и проверки предусловий."""

    async def test_polls_until_terminal(self) -> None:
        clock = FakeClock()
        collector = FakeCollector(statuses=["RUNNING", "RUNNING", "COMPLETED"])
        view = await run_collect(
            collector, JOB_ID, "запрос",
            fallback_expansion("запрос", GLOSSARY), CollectConfig(), clock, no_check,
        )
        self.assertEqual(view.status, "COMPLETED")
        self.assertEqual(len(clock.slept), 2)

    async def test_too_few_documents(self) -> None:
        collector = FakeCollector(documents_total=5, statuses=["COMPLETED"])
        with self.assertRaises(StageFailed) as error:
            await run_collect(
                collector, JOB_ID, "запрос", fallback_expansion("запрос", GLOSSARY),
                CollectConfig(min_documents=20), FakeClock(), no_check,
            )
        self.assertEqual(error.exception.error_code, "TOO_FEW_DOCUMENTS")

    async def test_failed_collection(self) -> None:
        collector = FakeCollector(statuses=["FAILED"])
        with self.assertRaises(StageFailed) as error:
            await run_collect(
                collector, JOB_ID, "запрос", fallback_expansion("запрос", GLOSSARY),
                CollectConfig(), FakeClock(), no_check,
            )
        self.assertEqual(error.exception.error_code, "COLLECTION_FAILED")

    async def test_upstream_failure_is_retryable(self) -> None:
        collector = FakeCollector(fail_start=True)
        with self.assertRaises(StageFailed) as error:
            await run_collect(
                collector, JOB_ID, "запрос", fallback_expansion("запрос", GLOSSARY),
                CollectConfig(), FakeClock(), no_check,
            )
        self.assertTrue(error.exception.retryable)

    async def test_reuses_existing_collection(self) -> None:
        collector = FakeCollector(statuses=["COMPLETED"])
        await run_collect(
            collector, JOB_ID, "запрос", fallback_expansion("запрос", GLOSSARY),
            CollectConfig(), FakeClock(), no_check, existing_collection_id=new_id(77),
        )
        self.assertEqual(collector.started, [])


class AnalyzeStageTest(unittest.IsolatedAsyncioTestCase):
    """Анализ коллекции: ожидание завершения и обработка отказа."""

    async def test_waits_for_completion(self) -> None:
        analyzer = FakeAnalyzer(statuses=["RUNNING", "COMPLETED"], candidates=[make_candidate(1, rank=1)])
        view = await run_analyze(
            analyzer, JOB_ID, new_id(10), "запрос", 15, AnalyzeConfig(), FakeClock(), no_check
        )
        self.assertEqual(view.status, "COMPLETED")
        self.assertEqual(view.weak_signals_total, 1)

    async def test_failed_analysis(self) -> None:
        analyzer = FakeAnalyzer(statuses=["FAILED"])
        with self.assertRaises(StageFailed) as error:
            await run_analyze(
                analyzer, JOB_ID, new_id(10), "запрос", 15, AnalyzeConfig(), FakeClock(), no_check
            )
        self.assertEqual(error.exception.error_code, "ANALYSIS_FAILED")

    async def test_timeout(self) -> None:
        analyzer = FakeAnalyzer(statuses=["RUNNING"])
        with self.assertRaises(StageFailed):
            await run_analyze(
                analyzer, JOB_ID, new_id(10), "запрос", 15,
                AnalyzeConfig(timeout_seconds=5, poll_seconds=3), FakeClock(), no_check,
            )


class TruncateTest(unittest.TestCase):
    """Усечение текста доказательства по границе предложения."""

    def test_keeps_short_text(self) -> None:
        self.assertEqual(truncate_at_sentence("Короткий текст.", 100), "Короткий текст.")

    def test_cuts_at_sentence_boundary(self) -> None:
        text = "Первое предложение. Второе предложение. Третье предложение."
        result = truncate_at_sentence(text, 40)
        self.assertTrue(result.endswith("."))
        self.assertLessEqual(len(result), 40)

    def test_cuts_at_word_when_no_sentence_end(self) -> None:
        result = truncate_at_sentence("слово " * 50, 30)
        self.assertLessEqual(len(result), 30)
        self.assertFalse(result.endswith("сло"))

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(truncate_at_sentence("  два   пробела  ", 100), "два пробела")


class NarrateStageTest(unittest.IsolatedAsyncioTestCase):
    """Сборка элементов выдачи и запись исключённых кандидатов."""

    def setUp(self) -> None:
        self.jobs = InMemoryJobRepository()
        self.results = InMemoryResultRepository(self.jobs)
        self.documents = {index: make_document(index) for index in (1, 2)}
        self.collector = FakeCollector(
            documents={document.document_id: document for document in self.documents.values()}
        )
        self.analyzer = FakeAnalyzer(
            candidates=[
                make_candidate(1, rank=1, score=0.91),
                make_candidate(2, rank=2, score=0.78),
                make_candidate(3, decision=Decision.MATURE, score=0.2),
            ]
        )

    async def test_writes_items_and_excluded(self) -> None:
        outcome = await run_narrate(
            analyzer=self.analyzer, collector=self.collector, insight=FakeInsight(),
            results=self.results, job_id=JOB_ID, query_text="запрос",
            analysis_id=new_id(20), config=NarrateConfig(top_n=15), check=no_check,
        )
        self.assertEqual(outcome.items_written, 2)
        self.assertEqual(outcome.narratives_generated, 2)
        self.assertEqual(outcome.excluded_written, 1)
        self.assertEqual(outcome.weak_signals_total, 2)
        self.assertEqual(outcome.weak_signals_confident, 2)
        items = self.results.items[JOB_ID]
        self.assertEqual([item.rank for item in items], [1, 2])
        self.assertTrue(all(item.sources for item in items))

    async def test_respects_top_n(self) -> None:
        outcome = await run_narrate(
            analyzer=self.analyzer, collector=self.collector, insight=FakeInsight(),
            results=self.results, job_id=JOB_ID, query_text="запрос",
            analysis_id=new_id(20), config=NarrateConfig(top_n=1), check=no_check,
        )
        self.assertEqual(outcome.items_written, 1)

    async def test_fallback_when_insight_unavailable(self) -> None:
        outcome = await run_narrate(
            analyzer=self.analyzer, collector=self.collector,
            insight=FakeInsight(fail_generate=True), results=self.results,
            job_id=JOB_ID, query_text="запрос", analysis_id=new_id(20),
            config=NarrateConfig(top_n=15), check=no_check,
        )
        self.assertEqual(outcome.narratives_fallback, 2)
        item = self.results.items[JOB_ID][0]
        self.assertIs(item.narrative_status, NarrativeStatus.FALLBACK_EXTRACTIVE)
        self.assertEqual(item.llm_provider, "none")
        self.assertTrue(item.explanation_ru)

    async def test_without_insight_service(self) -> None:
        outcome = await run_narrate(
            analyzer=self.analyzer, collector=self.collector, insight=None,
            results=self.results, job_id=JOB_ID, query_text="запрос",
            analysis_id=new_id(20), config=NarrateConfig(top_n=15), check=no_check,
        )
        self.assertEqual(outcome.items_written, 2)
        self.assertEqual(outcome.narratives_fallback, 2)

    async def test_repeat_does_not_duplicate_items(self) -> None:
        for _ in range(2):
            await run_narrate(
                analyzer=FakeAnalyzer(candidates=self.analyzer.candidates), collector=self.collector,
                insight=FakeInsight(), results=self.results, job_id=JOB_ID, query_text="запрос",
                analysis_id=new_id(20), config=NarrateConfig(top_n=15), check=no_check,
            )
        self.assertEqual(len(self.results.items[JOB_ID]), 2)
        self.assertEqual(len(self.results.excluded[JOB_ID]), 1)

    def test_sources_carry_required_fields(self) -> None:
        candidate = make_candidate(1, rank=1)
        documents = {document.document_id: document for document in self.documents.values()}
        sources = build_sources(candidate, documents, {})
        self.assertEqual([source.position for source in sources], [1, 2])
        for source in sources:
            self.assertTrue(source.title and source.url and source.language_code)
            self.assertIn(source.summary_kind, set(SummaryKind))
            self.assertTrue(source.summary_ru)

    def test_features_get_display_order(self) -> None:
        rows = build_features(make_candidate(1, rank=1))
        self.assertEqual([row.display_order for row in rows], [1, 2])


class FinalizeStageTest(unittest.IsolatedAsyncioTestCase):
    """Статистика и итоговый статус."""

    async def test_stats_and_status(self) -> None:
        jobs = InMemoryJobRepository()
        results = InMemoryResultRepository(jobs)
        collector = FakeCollector(
            statuses=["COMPLETED"],
            documents={make_document(1).document_id: make_document(1),
                       make_document(2).document_id: make_document(2)},
        )
        analyzer = FakeAnalyzer(candidates=[make_candidate(1, rank=1)])
        outcome = await run_narrate(
            analyzer=analyzer, collector=collector, insight=FakeInsight(), results=results,
            job_id=JOB_ID, query_text="запрос", analysis_id=new_id(20),
            config=NarrateConfig(top_n=1), check=no_check,
        )
        collection = await collector.get_collection(new_id(10))
        analysis = await analyzer.get_analysis(new_id(20))
        stats = build_stats(collection, analysis, outcome, None, {"collect": 1000, "narrate": 500})
        self.assertEqual(stats.documents_collected, 120)
        self.assertEqual(stats.http_requests_total, 1200)
        self.assertEqual(stats.collect_ms, 1000)
        self.assertEqual(stats.model_version_id, "wsclf-2026.09.16-1")
        completion = decide_final_status(outcome, 1, collection)
        self.assertIs(completion.status, JobStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
