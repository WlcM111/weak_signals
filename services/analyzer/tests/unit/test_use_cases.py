"""Прикладные сценарии analyzer: запуск, чтение, отмена, скоринг текста, активация модели."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.adapters.outbound.model_store import FileSystemModelStore
from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import CollectionInfo, EncyclopediaHit, StartAnalysisCommand
from analyzer.application.use_cases.activate_model import ActivateModelFromStore
from analyzer.application.use_cases.cancel_analysis import CancelAnalysis
from analyzer.application.use_cases.get_analysis import GetAnalysis
from analyzer.application.use_cases.get_model_info import GetModelInfo
from analyzer.application.use_cases.list_candidates import ListCandidates
from analyzer.application.use_cases.score_text import ScoreText
from analyzer.application.use_cases.start_analysis import StartAnalysis
from analyzer.domain.entities import Candidate
from analyzer.domain.errors import (
    CollectorUnavailable,
    InvariantViolation,
    ModelNotLoaded,
    NotFoundError,
    PreconditionFailedError,
    ResourceExhaustedError,
)
from analyzer.domain.values import Decision, DecisionReason, OperationStatus, TrustLevel
from ..fakes import (
    NOW,
    FakeClock,
    FakeCollector,
    FakeEmbedder,
    InMemoryAnalysisRepository,
    InMemoryCandidateRepository,
    InMemoryModelVersionRepository,
    make_document,
)
from ..model_fixture import EMBEDDING_MODEL, MODEL_VERSION_ID, build_model_store

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = load_feature_registry(REPO_ROOT / "schemas" / "feature_registry_v1.json")
LEXICONS = load_lexicons(
    REPO_ROOT / "services" / "analyzer" / "config" / "lexicons",
    REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
)
COLLECTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNKNOWN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class ModelHarness(unittest.TestCase):
    """Общая загрузка настоящего артефакта модели."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        build_model_store(cls.root, REGISTRY.model_names)
        cls.bundle = FileSystemModelStore(cls.root).load_active(REGISTRY)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()


class ActivateModelTest(ModelHarness):
    """Активация модели: регистрация версии и проверки совместимости."""

    def test_registers_and_publishes(self) -> None:
        versions = InMemoryModelVersionRepository()
        holder = ActiveModelHolder()
        use_case = ActivateModelFromStore(
            FileSystemModelStore(self.root), REGISTRY, versions, holder, FakeEmbedder()
        )
        bundle = use_case.execute()
        self.assertEqual(bundle.version.model_version_id, MODEL_VERSION_ID)
        self.assertTrue(holder.is_loaded)
        self.assertEqual(versions.get_active().model_version_id, MODEL_VERSION_ID)
        self.assertEqual(versions.get_active().embedding_model, EMBEDDING_MODEL)

    def test_rejects_embedder_mismatch(self) -> None:
        class OtherEmbedder(FakeEmbedder):
            """Эмбеддер другой модели."""

            model_name_value = "intfloat/multilingual-e5-small"

        use_case = ActivateModelFromStore(
            FileSystemModelStore(self.root),
            REGISTRY,
            InMemoryModelVersionRepository(),
            ActiveModelHolder(),
            OtherEmbedder(),
        )
        with self.assertRaises(InvariantViolation):
            use_case.execute()

    def test_holder_without_model_raises(self) -> None:
        with self.assertRaises(ModelNotLoaded):
            ActiveModelHolder().get()


class StartAnalysisTest(ModelHarness):
    """Запуск анализа: предусловия, идемпотентность, лимит очереди."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.analyses = InMemoryAnalysisRepository(self.clock)
        self.collector = FakeCollector(documents=[make_document(1, "документ")])
        self.holder = ActiveModelHolder(self.bundle)
        self.use_case = StartAnalysis(self.analyses, self.collector, self.holder, max_pending=20)

    def command(self, key: str = "job-1:analyze") -> StartAnalysisCommand:
        """Валидная команда запуска."""
        return StartAnalysisCommand(
            idempotency_key=key,
            collection_id=COLLECTION_ID,
            query_text="нейроморфные вычисления",
            params_raw=(0, 0, 0.0, 0),
        )

    def test_creates_pending_analysis(self) -> None:
        result = self.use_case.execute(self.command())
        self.assertIs(result.status, OperationStatus.PENDING)
        self.assertFalse(result.already_existed)
        self.assertEqual(result.model_version_id, MODEL_VERSION_ID)
        analysis = self.analyses.get(result.analysis_id)
        self.assertEqual(analysis.params.top_n, 15)
        self.assertEqual(analysis.params.weak_signal_threshold, self.bundle.threshold)

    def test_repeated_key_returns_existing(self) -> None:
        first = self.use_case.execute(self.command())
        second = self.use_case.execute(self.command())
        self.assertTrue(second.already_existed)
        self.assertEqual(first.analysis_id, second.analysis_id)

    def test_rejects_non_terminal_collection(self) -> None:
        self.collector.collection = CollectionInfo(COLLECTION_ID, "RUNNING", 10, False)
        with self.assertRaises(PreconditionFailedError) as error:
            self.use_case.execute(self.command())
        self.assertEqual(error.exception.error_code, "COLLECTION_NOT_TERMINAL")

    def test_rejects_empty_collection(self) -> None:
        self.collector.collection = CollectionInfo(COLLECTION_ID, "COMPLETED", 0, True)
        with self.assertRaises(PreconditionFailedError) as error:
            self.use_case.execute(self.command())
        self.assertEqual(error.exception.error_code, "COLLECTION_EMPTY")

    def test_requires_active_model(self) -> None:
        use_case = StartAnalysis(self.analyses, self.collector, ActiveModelHolder(), 20)
        with self.assertRaises(ModelNotLoaded):
            use_case.execute(self.command())

    def test_queue_limit(self) -> None:
        use_case = StartAnalysis(self.analyses, self.collector, self.holder, max_pending=1)
        use_case.execute(self.command("job-1:analyze"))
        with self.assertRaises(ResourceExhaustedError):
            use_case.execute(self.command("job-2:analyze"))

    def test_collector_unavailable_propagates(self) -> None:
        class Broken(FakeCollector):
            """Недоступный collector."""

            def get_collection(self, collection_id: str) -> CollectionInfo:
                raise CollectorUnavailable("collector.GetCollection: UNAVAILABLE")

        use_case = StartAnalysis(self.analyses, Broken(), self.holder, 20)
        with self.assertRaises(CollectorUnavailable):
            use_case.execute(self.command())


class GetAndCancelTest(ModelHarness):
    """Чтение состояния и кооперативная отмена."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.analyses = InMemoryAnalysisRepository(self.clock)
        collector = FakeCollector(documents=[make_document(1, "документ")])
        start = StartAnalysis(self.analyses, collector, ActiveModelHolder(self.bundle), 20)
        self.analysis_id = start.execute(
            StartAnalysisCommand("job-1:analyze", COLLECTION_ID, "запрос", (0, 0, 0.0, 0))
        ).analysis_id

    def test_get_returns_view(self) -> None:
        view = GetAnalysis(self.analyses).execute(self.analysis_id)
        self.assertIs(view.status, OperationStatus.PENDING)
        self.assertEqual(view.model_version_id, MODEL_VERSION_ID)

    def test_get_unknown(self) -> None:
        with self.assertRaises(NotFoundError):
            GetAnalysis(self.analyses).execute(UNKNOWN_ID)

    def test_cancel_pending_becomes_cancelled(self) -> None:
        status = CancelAnalysis(self.analyses).execute(self.analysis_id, "пользователь отменил")
        self.assertIs(status, OperationStatus.CANCELLED)

    def test_cancel_is_idempotent(self) -> None:
        cancel = CancelAnalysis(self.analyses)
        cancel.execute(self.analysis_id, "первый раз")
        self.assertIs(cancel.execute(self.analysis_id, "второй раз"), OperationStatus.CANCELLED)

    def test_cancel_unknown(self) -> None:
        with self.assertRaises(NotFoundError):
            CancelAnalysis(self.analyses).execute(UNKNOWN_ID, "")


class ListCandidatesTest(ModelHarness):
    """Пагинация и порядок выдачи кандидатов."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.analyses = InMemoryAnalysisRepository(self.clock)
        self.candidates = InMemoryCandidateRepository(self.analyses, self.clock)
        collector = FakeCollector(documents=[make_document(1, "документ")])
        start = StartAnalysis(self.analyses, collector, ActiveModelHolder(self.bundle), 20)
        self.analysis_id = start.execute(
            StartAnalysisCommand("job-1:analyze", COLLECTION_ID, "запрос", (0, 0, 0.0, 0))
        ).analysis_id
        self.analyses.claim_next("worker-1", 60)
        rows = [
            self._candidate(0, Decision.WEAK_SIGNAL, 0.9, rank=1),
            self._candidate(1, Decision.WEAK_SIGNAL, 0.8, rank=2),
            self._candidate(2, Decision.MATURE, 0.4),
            self._candidate(3, Decision.OFF_TOPIC, 0.1),
        ]
        self.candidates.save_results(self.analysis_id, "worker-1", rows, self.analyses.get(self.analysis_id).stats, NOW)
        self.use_case = ListCandidates(self.analyses, self.candidates)

    @staticmethod
    def _candidate(index: int, decision: Decision, score: float, rank: int = 0) -> Candidate:
        """Кандидат для проверки порядка."""
        reason = (
            DecisionReason.MODEL_SCORE
            if decision is Decision.WEAK_SIGNAL
            else DecisionReason.MATURITY_LEXICON
        )
        return Candidate(
            cluster_index=index,
            title_auto=f"кандидат {index}",
            keyphrases=("фраза",),
            score=score,
            decision=decision,
            decision_reason=reason,
            decision_explanation_ru="объяснение",
            features=(),
            documents=(),
            document_count=2,
            query_relevance=0.5,
            rank=rank,
        )

    def test_only_weak_signals_by_default(self) -> None:
        page = self.use_case.execute(
            self.analysis_id, include_excluded=False, page_size=50, after=None
        )
        self.assertEqual([item.rank for item in page.candidates], [1, 2])
        self.assertEqual(page.total_count, 2)
        self.assertEqual(page.next_page_token, "")

    def test_excluded_follow_weak_signals(self) -> None:
        page = self.use_case.execute(
            self.analysis_id, include_excluded=True, page_size=50, after=None
        )
        decisions = [item.decision for item in page.candidates]
        self.assertEqual(decisions[:2], [Decision.WEAK_SIGNAL, Decision.WEAK_SIGNAL])
        self.assertEqual(decisions[2:], [Decision.MATURE, Decision.OFF_TOPIC])
        self.assertEqual(page.total_count, 4)

    def test_pagination_is_stable(self) -> None:
        first = self.use_case.execute(
            self.analysis_id, include_excluded=True, page_size=2, after=None
        )
        self.assertTrue(first.next_page_token)
        from analyzer.application.validation import validate_page_token

        second = self.use_case.execute(
            self.analysis_id,
            include_excluded=True,
            page_size=2,
            after=validate_page_token(first.next_page_token),
        )
        seen = [item.cluster_index for item in (*first.candidates, *second.candidates)]
        self.assertEqual(seen, [0, 1, 2, 3])

    def test_unknown_analysis(self) -> None:
        with self.assertRaises(NotFoundError):
            self.use_case.execute(UNKNOWN_ID, include_excluded=False, page_size=10, after=None)


class GetModelInfoTest(ModelHarness):
    """Сведения о модели содержат порядок признаков реестра."""

    def test_returns_version_and_features(self) -> None:
        version, names = GetModelInfo(ActiveModelHolder(self.bundle)).execute()
        self.assertEqual(version.model_version_id, MODEL_VERSION_ID)
        self.assertEqual(names, REGISTRY.names)
        self.assertEqual(len(names), 25)

    def test_without_model(self) -> None:
        with self.assertRaises(ModelNotLoaded):
            GetModelInfo(ActiveModelHolder()).execute()


class ScoreTextTest(ModelHarness):
    """Прямой скоринг описания: без обогащения и с обогащением."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.collector = FakeCollector(documents=[])
        self.use_case = ScoreText(
            self.collector,
            FakeEmbedder(),
            ActiveModelHolder(self.bundle),
            LEXICONS,
            self.clock,
            enrichment_timeout_seconds=30.0,
        )

    def test_without_enrichment(self) -> None:
        result = self.use_case.execute(
            "Нейроморфные чипы", "Прототип показал энергоэффективность", with_enrichment=False
        )
        self.assertFalse(result.enrichment_applied)
        self.assertEqual(len(result.features), 25)
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 1.0)
        self.assertIsInstance(result.decision, Decision)

    def test_uses_manifest_defaults(self) -> None:
        result = self.use_case.execute("Технология", "", with_enrichment=False)
        recency = next(
            item for item in result.features if item.feature_name == "recency_median_days"
        )
        self.assertEqual(recency.value, 400.0)

    def test_off_topic_rule_never_fires(self) -> None:
        result = self.use_case.execute("Тема", "описание", with_enrichment=False)
        self.assertIsNot(result.decision, Decision.OFF_TOPIC)
        similarity = next(
            item for item in result.features if item.feature_name == "emb_sim_query"
        )
        self.assertEqual(similarity.value, 1.0)

    def test_with_enrichment_uses_collection(self) -> None:
        documents = [
            make_document(index, "Нейроморфные чипы", "прототип и пилот", trust_level=TrustLevel.HIGH)
            for index in range(1, 4)
        ]
        self.collector.documents = documents
        self.collector.encyclopedia = {
            "нейроморфные чипы": EncyclopediaHit("Нейроморфные чипы", True, pageviews_30d=100)
        }
        result = self.use_case.execute("Нейроморфные чипы", "описание", with_enrichment=True)
        self.assertTrue(result.enrichment_applied)
        doc_count = next(item for item in result.features if item.feature_name == "doc_count_log")
        self.assertGreater(doc_count.value, 0.0)
        wiki = next(item for item in result.features if item.feature_name == "wiki_exists")
        self.assertEqual(wiki.value, 1.0)

    def test_enrichment_failure_degrades_gracefully(self) -> None:
        class Broken(FakeCollector):
            """Collector недоступен при обогащении."""

            def start_enrichment(self, idempotency_key: str, title: str) -> str:
                raise CollectorUnavailable("collector.StartCollection: UNAVAILABLE")

        use_case = ScoreText(
            Broken(), FakeEmbedder(), ActiveModelHolder(self.bundle), LEXICONS, self.clock
        )
        result = use_case.execute("Тема", "описание", with_enrichment=True)
        self.assertFalse(result.enrichment_applied)
        self.assertEqual(len(result.features), 25)

    def test_requires_active_model(self) -> None:
        use_case = ScoreText(
            self.collector, FakeEmbedder(), ActiveModelHolder(), LEXICONS, self.clock
        )
        with self.assertRaises(ModelNotLoaded):
            use_case.execute("Тема", "", with_enrichment=False)


if __name__ == "__main__":
    unittest.main()
