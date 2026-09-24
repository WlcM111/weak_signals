"""Конвейер RunAnalysis целиком: от документов коллекции до записанных кандидатов (§12.5 ТЗ).

Модель — настоящий артефакт из `model_fixture`, эмбеддер и collector — детерминированные дублёры.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.adapters.outbound.model_store import FileSystemModelStore
from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import AnalysisDraft, EncyclopediaHit
from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig
from analyzer.domain.errors import CollectorUnavailable, LeaseLost
from analyzer.domain.values import (
    AnalysisParams,
    Decision,
    DecisionReason,
    OperationStatus,
    SourceType,
    TrustLevel,
)
from ..fakes import (
    NOW,
    FakeClock,
    FakeCollector,
    FakeEmbedder,
    InMemoryAnalysisRepository,
    InMemoryCandidateRepository,
    InMemoryEmbeddingCache,
    make_document,
)
from ..model_fixture import build_model_store

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = load_feature_registry(REPO_ROOT / "schemas" / "feature_registry_v1.json")
LEXICONS = load_lexicons(
    REPO_ROOT / "services" / "analyzer" / "config" / "lexicons",
    REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
)
QUERY = "нейроморфные вычисления в промышленности"
TOPICS = {"нейроморф": 0, "контейнер": 3, "платформ": 5}
COLLECTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EMBEDDING_DIMS = 64  # размерность дублёра эмбеддера: группы должны разделяться кластеризацией


def emerging_documents() -> list:
    """Кластер слабого сигнала: научные источники, лексика ранней стадии, свежие даты."""
    text = (
        "Исследователи собрали прототип нейроморфного процессора и запустили пилотные испытания "
        "в 2026 году. Лабораторный образец показал энергоэффективность на порядок выше. "
        "Посевной раунд инвестиций на $4 млн закрыт."
    )
    return [
        make_document(
            index,
            f"Нейроморфные чипы: прототип {index}",
            text,
            source_type=SourceType.SCIENTIFIC_PUBLICATION if index % 2 else SourceType.PREPRINT,
            trust_level=TrustLevel.HIGH,
            published_at=NOW - timedelta(days=30 * index),
            citation_count=index,
        )
        for index in range(1, 5)
    ]


def mature_documents() -> list:
    """Кластер зрелой технологии: лексика массового внедрения и крупные вендоры."""
    text = (
        "Контейнеризация стала отраслевым стандартом: массовое внедрение идёт повсеместно, "
        "рынок сформирован. Google, Microsoft и Amazon поставляют решение промышленной "
        "эксплуатации, доля рынка растёт с 2016 года."
    )
    return [
        make_document(
            10 + index,
            f"Контейнерные платформы в проде {index}",
            text,
            source_type=SourceType.INDUSTRY_MEDIA,
            trust_level=TrustLevel.MEDIUM,
            published_at=NOW - timedelta(days=400 + index),
        )
        for index in range(1, 4)
    ]


def marketing_documents() -> list:
    """Кластер маркетингового шума: пресс-релизы и блоги без научных источников."""
    text = (
        "Революционная платформа не имеет аналогов и меняет всё. Первый в мире продукт, "
        "беспрецедентный результат — уверяют в компании."
    )
    return [
        make_document(
            20 + index,
            f"Платформа будущего: релиз {index}",
            text,
            source_type=SourceType.PRESS_RELEASE if index % 2 else SourceType.CORPORATE_BLOG,
            trust_level=TrustLevel.MEDIUM if index == 1 else TrustLevel.LOW,
            published_at=NOW - timedelta(days=10 * index),
        )
        for index in range(1, 4)
    ]


class RunAnalysisHarness(unittest.TestCase):
    """Общая сборка конвейера с настоящей моделью и дублёрами портов."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        build_model_store(Path(cls._tmp.name), REGISTRY.model_names, dims=EMBEDDING_DIMS)
        cls.bundle = FileSystemModelStore(Path(cls._tmp.name)).load_active(REGISTRY)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def build(
        self,
        documents: list,
        *,
        threshold: float = 0.01,
        config: RunAnalysisConfig | None = None,
        collector: FakeCollector | None = None,
    ) -> tuple[RunAnalysis, object, InMemoryAnalysisRepository, InMemoryCandidateRepository, FakeCollector]:
        """Создаёт use case, захваченный анализ и репозитории."""
        clock = FakeClock()
        analyses = InMemoryAnalysisRepository(clock)
        candidates = InMemoryCandidateRepository(analyses, clock)
        cache = InMemoryEmbeddingCache()
        self.cache = cache
        self.embedder = FakeEmbedder(dims=EMBEDDING_DIMS, topic_terms=TOPICS, base_axis=0, base_weight=0.6, jitter=0.7)
        reader = collector or FakeCollector(documents=documents)
        holder = ActiveModelHolder(self.bundle)
        use_case = RunAnalysis(
            analyses=analyses,
            candidates=candidates,
            embedding_cache=cache,
            collector=reader,
            embedder=self.embedder,
            active_model=holder,
            lexicons=LEXICONS,
            clock=clock,
            config=config or RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2),
        )
        analysis, _ = analyses.create_if_absent(
            AnalysisDraft(
                idempotency_key="job-1:analyze",
                collection_id=COLLECTION_ID,
                query_text=QUERY,
                model_version_id=self.bundle.version.model_version_id,
                params=AnalysisParams.from_request(self.bundle.threshold, weak_signal_threshold=threshold),
            )
        )
        analyses.claim_next("worker-1", 60)
        self.clock = clock
        return use_case, analysis, analyses, candidates, reader


class EndToEndTest(RunAnalysisHarness):
    """Три тематические группы документов → три кандидата с разными решениями."""

    def setUp(self) -> None:
        documents = emerging_documents() + mature_documents() + marketing_documents()
        self.use_case, self.analysis, self.analyses, self.candidates, self.collector = self.build(
            documents
        )
        self.status = self.use_case.execute(self.analysis, "worker-1")
        self.saved = self.candidates.saved[self.analysis.analysis_id]

    def test_completes(self) -> None:
        self.assertIs(self.status, OperationStatus.COMPLETED)
        self.assertIs(self.analysis.status, OperationStatus.COMPLETED)

    def test_three_candidates(self) -> None:
        self.assertEqual(len(self.saved), 3)
        self.assertEqual(self.analysis.stats.clusters_total, 3)
        self.assertEqual(self.analysis.stats.candidates_scored, 3)

    def test_documents_counted(self) -> None:
        self.assertEqual(self.analysis.stats.documents_input, 10)
        self.assertEqual(self.analysis.stats.documents_after_dedup, 10)

    def _by_group(self, first_index: int):  # noqa: ANN202 - Candidate
        """Кандидат, собравший документы конкретной синтетической группы."""
        prefix = f"{first_index:08d}"
        return next(
            candidate
            for candidate in self.saved
            if any(document.document_id.startswith(prefix) for document in candidate.documents)
        )

    def test_decisions_by_cluster(self) -> None:
        emerging = self._by_group(1)
        mature = self._by_group(11)
        marketing = self._by_group(21)
        self.assertIs(emerging.decision, Decision.WEAK_SIGNAL)
        self.assertIs(mature.decision, Decision.MATURE)
        self.assertIs(marketing.decision, Decision.HYPE_OR_NOISE)
        self.assertIs(marketing.decision_reason, DecisionReason.MARKETING_DOMINANT)

    def test_every_candidate_has_full_feature_vector(self) -> None:
        for candidate in self.saved:
            self.assertEqual(len(candidate.features), 25)
            self.assertEqual(
                {feature.feature_name for feature in candidate.features}, set(REGISTRY.names)
            )

    def test_features_sorted_by_absolute_contribution(self) -> None:
        for candidate in self.saved:
            contributions = [abs(feature.contribution) for feature in candidate.features]
            self.assertEqual(contributions, sorted(contributions, reverse=True))

    def test_emb_sim_query_has_no_contribution(self) -> None:
        for candidate in self.saved:
            feature = next(
                item for item in candidate.features if item.feature_name == "emb_sim_query"
            )
            self.assertEqual(feature.contribution, 0.0)

    def test_evidence_limits_and_trust(self) -> None:
        for candidate in self.saved:
            evidence = candidate.evidence
            self.assertGreaterEqual(len(evidence), 1)
            self.assertLessEqual(len(evidence), 8)
            self.assertTrue(
                any(item.trust_level in (TrustLevel.HIGH, TrustLevel.MEDIUM) for item in evidence)
            )
            for item in evidence:
                self.assertLessEqual(len(item.snippet), 600)
                self.assertTrue(item.snippet)

    def test_explanations_are_russian_and_bounded(self) -> None:
        for candidate in self.saved:
            self.assertTrue(candidate.decision_explanation_ru)
            self.assertLessEqual(len(candidate.decision_explanation_ru), 500)
            self.assertNotIn("score", candidate.decision_explanation_ru.lower())

    def test_ranks_are_unique_and_continuous(self) -> None:
        ranks = sorted(item.rank for item in self.saved if item.decision is Decision.WEAK_SIGNAL)
        self.assertEqual(ranks, list(range(1, len(ranks) + 1)))
        for candidate in self.saved:
            if candidate.decision is not Decision.WEAK_SIGNAL:
                self.assertEqual(candidate.rank, 0)

    def test_stats_match_decisions(self) -> None:
        stats = self.analysis.stats
        self.assertEqual(stats.weak_signals_total, 1)
        self.assertEqual(stats.excluded_mature, 1)
        self.assertEqual(stats.excluded_hype_or_noise, 1)

    def test_embeddings_are_cached(self) -> None:
        self.assertEqual(len(self.cache.entries), 10)

    def test_documents_carry_similarity_and_counts(self) -> None:
        for candidate in self.saved:
            self.assertEqual(candidate.document_count, len(candidate.documents))
            self.assertTrue(all(0.0 <= item.similarity <= 1.0 for item in candidate.documents))
            self.assertEqual(sum(candidate.source_type_counts.values()), candidate.document_count)


class CacheReuseTest(RunAnalysisHarness):
    """Повторный анализ той же коллекции берёт эмбеддинги из кеша."""

    def test_second_run_computes_nothing(self) -> None:
        documents = emerging_documents() + mature_documents()
        use_case, analysis, analyses, _, _ = self.build(documents)
        use_case.execute(analysis, "worker-1")
        first_calls = len([call for call in self.embedder.calls if call[1] == "passage: "])
        cache = self.cache

        # второй анализ поверх наполненного кеша
        clock = FakeClock()
        analyses2 = InMemoryAnalysisRepository(clock)
        candidates2 = InMemoryCandidateRepository(analyses2, clock)
        embedder2 = FakeEmbedder(dims=EMBEDDING_DIMS, topic_terms=TOPICS, base_axis=0, base_weight=0.6, jitter=0.7)
        use_case2 = RunAnalysis(
            analyses=analyses2,
            candidates=candidates2,
            embedding_cache=cache,
            collector=FakeCollector(documents=documents),
            embedder=embedder2,
            active_model=ActiveModelHolder(self.bundle),
            lexicons=LEXICONS,
            clock=clock,
            config=RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2),
        )
        analysis2, _ = analyses2.create_if_absent(
            AnalysisDraft(
                idempotency_key="job-2:analyze",
                collection_id=COLLECTION_ID,
                query_text=QUERY,
                model_version_id=self.bundle.version.model_version_id,
                params=AnalysisParams.from_request(0.5, weak_signal_threshold=0.01),
            )
        )
        analyses2.claim_next("worker-2", 60)
        use_case2.execute(analysis2, "worker-2")
        self.assertGreater(first_calls, 0)
        self.assertEqual([call for call in embedder2.calls if call[1] == "passage: "], [])

    def test_changed_text_invalidates_cache(self) -> None:
        documents = emerging_documents()
        use_case, analysis, _, _, _ = self.build(documents)
        use_case.execute(analysis, "worker-1")
        self.assertEqual(len(self.cache.entries), len(documents))
        stored = next(iter(self.cache.entries.values()))
        self.assertNotEqual(stored.content_hash, "")


class NearDupTest(RunAnalysisHarness):
    """Дубликаты исключаются из анализа."""

    def test_duplicates_are_dropped(self) -> None:
        documents = emerging_documents()
        duplicate = make_document(
            99,
            documents[0].title,
            documents[0].text,
            source_type=documents[0].source_type,
            trust_level=TrustLevel.LOW,
            published_at=documents[0].published_at,
        )
        use_case, analysis, _, candidates, _ = self.build([*documents, duplicate])
        use_case.execute(analysis, "worker-1")
        self.assertEqual(analysis.stats.documents_input, 5)
        self.assertEqual(analysis.stats.documents_after_dedup, 4)


class EncyclopediaRuleTest(RunAnalysisHarness):
    """Зрелая статья Wikipedia исключает кандидата правилом 4."""

    def test_encyclopedia_mature(self) -> None:
        documents = emerging_documents()
        titles = {
            title.lower(): EncyclopediaHit(
                title=title,
                exists=True,
                pageviews_30d=90000,
                created_at=NOW - timedelta(days=int(365.25 * 8)),
            )
            for title in ("нейроморфные чипы", "нейроморфные", "чипы")
        }
        collector = FakeCollector(documents=documents, encyclopedia=titles)
        use_case, analysis, _, candidates, _ = self.build(documents, collector=collector)
        use_case.execute(analysis, "worker-1")
        saved = candidates.saved[analysis.analysis_id]
        self.assertTrue(
            any(item.decision_reason is DecisionReason.ENCYCLOPEDIA_MATURE for item in saved),
            f"решения: {[item.decision_reason for item in saved]}",
        )


class FailurePathTest(RunAnalysisHarness):
    """Отказы конвейера: пустая коллекция, недоступный collector, фильтр релевантности."""

    def test_empty_collection(self) -> None:
        use_case, analysis, _, _, _ = self.build([])
        status = use_case.execute(analysis, "worker-1")
        self.assertIs(status, OperationStatus.FAILED)
        self.assertEqual(analysis.error_code, "NO_DOCUMENTS")

    def test_collector_unavailable(self) -> None:
        class BrokenCollector(FakeCollector):
            """Поток документов обрывается ошибкой."""

            def stream_documents(self, collection_id, chunk_size, limit):  # noqa: ANN001, ANN201
                raise CollectorUnavailable("collector.StreamDocuments: UNAVAILABLE")
                yield  # pragma: no cover - недостижимо, нужно для генератора

        use_case, analysis, _, _, _ = self.build([], collector=BrokenCollector())
        status = use_case.execute(analysis, "worker-1")
        self.assertIs(status, OperationStatus.FAILED)
        self.assertEqual(analysis.error_code, "COLLECTOR_UNAVAILABLE")

    def test_all_documents_filtered_by_relevance(self) -> None:
        documents = mature_documents()
        config = RunAnalysisConfig(min_doc_query_sim=0.99, min_cluster_size=2)
        use_case, analysis, _, _, _ = self.build(documents, config=config)
        status = use_case.execute(analysis, "worker-1")
        self.assertIs(status, OperationStatus.FAILED)
        self.assertEqual(analysis.error_code, "NO_DOCUMENTS")

    def test_truncation_limit(self) -> None:
        documents = emerging_documents() + mature_documents()
        config = RunAnalysisConfig(max_documents=3, min_doc_query_sim=0.0, min_cluster_size=1)
        use_case, analysis, _, _, _ = self.build(documents, config=config)
        use_case.execute(analysis, "worker-1")
        self.assertEqual(analysis.stats.documents_input, 3)


class CancellationTest(RunAnalysisHarness):
    """Отмена и потеря аренды между шагами конвейера."""

    def test_cancel_between_steps(self) -> None:
        documents = emerging_documents() + mature_documents()
        config = RunAnalysisConfig(heartbeat_seconds=0, min_doc_query_sim=0.25, min_cluster_size=2)
        use_case, analysis, analyses, candidates, _ = self.build(documents, config=config)
        analysis.cancel_requested = True
        status = use_case.execute(analysis, "worker-1")
        self.assertIs(status, OperationStatus.CANCELLED)
        self.assertEqual(analysis.error_code, "CANCELLED")
        self.assertNotIn(analysis.analysis_id, candidates.saved)

    def test_lease_lost_raises(self) -> None:
        documents = emerging_documents()
        config = RunAnalysisConfig(heartbeat_seconds=0, min_doc_query_sim=0.25, min_cluster_size=2)
        use_case, analysis, analyses, _, _ = self.build(documents, config=config)
        analyses.lease_alive = False
        with self.assertRaises(LeaseLost):
            use_case.execute(analysis, "worker-1")

    def test_lease_lost_on_save_is_reported(self) -> None:
        documents = emerging_documents()
        use_case, analysis, _, candidates, _ = self.build(documents)
        candidates.lease_alive = False
        with self.assertRaises(LeaseLost):
            use_case.execute(analysis, "worker-1")


if __name__ == "__main__":
    unittest.main()
