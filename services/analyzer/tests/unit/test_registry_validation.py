"""Реестр признаков, инварианты сущностей и валидация контракта analyzer."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from analyzer.adapters.outbound.config_loader import ConfigError, load_feature_registry, load_lexicons
from analyzer.domain.entities import Analysis, Candidate, DocumentRef, FeatureContribution
from analyzer.domain.errors import InvariantViolation, ValidationError
from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.values import (
    AnalysisParams,
    Decision,
    DecisionReason,
    FeatureDirection,
    OperationStatus,
    SourceType,
    TrustLevel,
)
from analyzer.application.validation import (
    encode_page_token,
    validate_cancel_reason,
    validate_idempotency_key,
    validate_page_size,
    validate_page_token,
    validate_params,
    validate_query_text,
    validate_score_text,
    validate_uuid,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_PATH = REPO_ROOT / "schemas" / "feature_registry_v1.json"
LEXICON_DIR = REPO_ROOT / "services" / "analyzer" / "config" / "lexicons"
STAGE_RULES = REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml"
UUID = "11111111-2222-4333-8444-555555555555"


class FeatureRegistryTest(unittest.TestCase):
    """Нормативный реестр: 25 признаков, 24 в модели, порядок и клиппинг."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_feature_registry(REGISTRY_PATH)

    def test_counts(self) -> None:
        self.assertEqual(len(self.registry.names), 25)
        self.assertEqual(len(self.registry.model_names), 24)

    def test_emb_sim_query_excluded_from_model(self) -> None:
        self.assertIn("emb_sim_query", self.registry.names)
        self.assertNotIn("emb_sim_query", self.registry.model_names)

    def test_model_order_matches_registry_order(self) -> None:
        expected = tuple(name for name in self.registry.names if name != "emb_sim_query")
        self.assertEqual(self.registry.model_names, expected)

    def test_clipping_by_range(self) -> None:
        spec = self.registry.spec("trusted_share")
        self.assertEqual(spec.clip(1.7), 1.0)
        self.assertEqual(spec.clip(-0.5), 0.0)

    def test_integer_features_are_rounded(self) -> None:
        spec = self.registry.spec("stage_lex_ordinal")
        self.assertEqual(spec.clip(3.4), 3.0)
        self.assertEqual(spec.clip(99.0), 5.0)

    def test_vector_requires_all_features(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.registry.vector({"lex_emergence_score": 0.5})

    def test_vector_length(self) -> None:
        values = dict.fromkeys(self.registry.names, 0.5)
        self.assertEqual(len(self.registry.vector(values)), 25)
        self.assertEqual(len(self.registry.model_vector(values)), 24)

    def test_rejects_foreign_schema_version(self) -> None:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        payload["feature_schema_version"] = "v2"
        with self.assertRaises(InvariantViolation):
            FeatureRegistry.from_mapping(payload)

    def test_rejects_wrong_feature_count(self) -> None:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        payload["features"] = payload["features"][:10]
        with self.assertRaises(InvariantViolation):
            FeatureRegistry.from_mapping(payload)


class LexiconLoaderTest(unittest.TestCase):
    """Лексиконы сервиса загружаются и содержательно наполнены."""

    def test_loads_all_groups(self) -> None:
        lexicons = load_lexicons(LEXICON_DIR, STAGE_RULES)
        self.assertGreater(len(lexicons.stopwords), 100)
        self.assertEqual(sorted(lexicons.stage_terms), [1, 2, 3, 4, 5])
        for group in (lexicons.emergence, lexicons.maturity, lexicons.hype, lexicons.bigtech):
            self.assertTrue(group.tokens or group.prefixes or group.phrases)

    def test_missing_directory_is_reported(self) -> None:
        with self.assertRaises(ConfigError):
            load_lexicons(LEXICON_DIR / "нет", STAGE_RULES)


class CandidateInvariantTest(unittest.TestCase):
    """Инварианты кандидата: ранг, длины, доказательства."""

    @staticmethod
    def build(**overrides: object) -> Candidate:
        """Кандидат с параметрами по умолчанию."""
        payload = {
            "cluster_index": 0,
            "title_auto": "название",
            "keyphrases": ("фраза",),
            "score": 0.8,
            "decision": Decision.WEAK_SIGNAL,
            "decision_reason": DecisionReason.MODEL_SCORE,
            "decision_explanation_ru": "объяснение",
            "features": (),
            "documents": (),
            "document_count": 2,
            "query_relevance": 0.7,
            "rank": 1,
        }
        payload.update(overrides)
        return Candidate(**payload)  # type: ignore[arg-type]

    def test_weak_signal_requires_rank(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(rank=0)

    def test_excluded_requires_zero_rank(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(decision=Decision.MATURE, decision_reason=DecisionReason.MATURITY_LEXICON, rank=3)

    def test_title_length_limit(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(title_auto="x" * 201)

    def test_keyphrases_limit(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(keyphrases=tuple(f"ф{index}" for index in range(11)))

    def test_explanation_limit(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(decision_explanation_ru="я" * 501)

    def test_score_range(self) -> None:
        with self.assertRaises(InvariantViolation):
            self.build(score=1.2)

    def test_feature_value_lookup(self) -> None:
        candidate = self.build(
            features=(
                FeatureContribution("trusted_share", 0.42, 0.1, "Доля", FeatureDirection.NEUTRAL),
            )
        )
        self.assertEqual(candidate.feature_value("trusted_share"), 0.42)
        self.assertEqual(candidate.feature_value("нет"), 0.0)


class DocumentRefTest(unittest.TestCase):
    """Текст для эмбеддинга и ключ инвалидации кеша."""

    def test_embedding_text_truncates_body(self) -> None:
        document = DocumentRef(
            document_id=UUID,
            title="Заголовок",
            text="т" * 2000,
            language_code="ru",
            source_type=SourceType.NEWS,
            trust_level=TrustLevel.MEDIUM,
            origin_domain="example.com",
        )
        self.assertTrue(document.embedding_text.startswith("Заголовок. "))
        self.assertEqual(len(document.embedding_text), len("Заголовок. ") + 1500)

    def test_content_key_changes_with_text(self) -> None:
        base = DocumentRef(
            document_id=UUID, title="A", text="один", language_code="ru",
            source_type=SourceType.NEWS, trust_level=TrustLevel.LOW, origin_domain="e.com",
        )
        changed = replace(base, text="другой")
        self.assertNotEqual(base.content_key(), changed.content_key())

    def test_explicit_content_hash_wins(self) -> None:
        document = DocumentRef(
            document_id=UUID, title="A", text="t", language_code="ru",
            source_type=SourceType.NEWS, trust_level=TrustLevel.LOW, origin_domain="e.com",
            content_hash="deadbeef",
        )
        self.assertEqual(document.content_key(), "deadbeef")


class AnalysisStateTest(unittest.TestCase):
    """Матрица переходов статусов анализа."""

    @staticmethod
    def build() -> Analysis:
        """Новый анализ в статусе PENDING."""
        return Analysis(
            analysis_id=UUID,
            idempotency_key="job:analyze",
            collection_id=UUID,
            query_text="запрос",
            model_version_id="wsclf-2026.09.15-1",
            params=AnalysisParams.from_request(0.5),
        )

    def test_allowed_transition(self) -> None:
        analysis = self.build()
        analysis.transition_to(OperationStatus.RUNNING, datetime.now(UTC))
        self.assertIs(analysis.status, OperationStatus.RUNNING)
        self.assertIsNotNone(analysis.started_at)

    def test_forbidden_transition(self) -> None:
        analysis = self.build()
        with self.assertRaises(InvariantViolation):
            analysis.transition_to(OperationStatus.COMPLETED, datetime.now(UTC))

    def test_terminal_is_final(self) -> None:
        analysis = self.build()
        now = datetime.now(UTC)
        analysis.transition_to(OperationStatus.RUNNING, now)
        analysis.finish(OperationStatus.FAILED, now, error_code="NO_DOCUMENTS")
        self.assertTrue(analysis.is_terminal)
        with self.assertRaises(InvariantViolation):
            analysis.transition_to(OperationStatus.RUNNING, now)

    def test_finish_requires_terminal_status(self) -> None:
        analysis = self.build()
        with self.assertRaises(InvariantViolation):
            analysis.finish(OperationStatus.RUNNING, datetime.now(UTC))


class AnalysisParamsTest(unittest.TestCase):
    """Значения по умолчанию параметров анализа."""

    def test_defaults(self) -> None:
        params = AnalysisParams.from_request(0.62)
        self.assertEqual((params.top_n, params.max_candidates), (15, 40))
        self.assertEqual(params.min_evidence_documents, 2)
        self.assertEqual(params.weak_signal_threshold, 0.62)

    def test_explicit_values_win(self) -> None:
        params = AnalysisParams.from_request(0.62, 5, 10, 0.8, 3)
        self.assertEqual((params.top_n, params.max_candidates), (5, 10))
        self.assertEqual(params.weak_signal_threshold, 0.8)


class ValidationTest(unittest.TestCase):
    """Валидация запросов RPC."""

    def test_uuid(self) -> None:
        self.assertEqual(validate_uuid(UUID, "analysis_id"), UUID)
        with self.assertRaises(ValidationError):
            validate_uuid("не-uuid", "analysis_id")

    def test_idempotency_key(self) -> None:
        self.assertEqual(validate_idempotency_key("job-123:analyze"), "job-123:analyze")
        for bad in ("", "short", "плохой ключ", "x" * 129):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                validate_idempotency_key(bad)

    def test_query_text(self) -> None:
        self.assertEqual(validate_query_text("  запрос  "), "запрос")
        for bad in ("", "a", "   ", "?" * 3, "x" * 501):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                validate_query_text(bad)

    def test_params_ranges(self) -> None:
        self.assertEqual(validate_params(0, 0, 0.0, 0), (0, 0, 0.0, 0))
        self.assertEqual(validate_params(15, 40, 0.5, 2), (15, 40, 0.5, 2))
        for args in ((51, 0, 0.0, 0), (0, 4, 0.0, 0), (0, 0, 1.0, 0), (0, 0, 0.0, -1)):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                validate_params(*args)

    def test_page_size(self) -> None:
        self.assertEqual(validate_page_size(0), 50)
        self.assertEqual(validate_page_size(200), 200)
        with self.assertRaises(ValidationError):
            validate_page_size(201)

    def test_page_token_roundtrip(self) -> None:
        token = encode_page_token(1, -0.75, 7)
        self.assertEqual(validate_page_token(token), (1, -0.75, 7))
        self.assertIsNone(validate_page_token(""))

    def test_broken_page_token(self) -> None:
        with self.assertRaises(ValidationError):
            validate_page_token("!!! не токен !!!")

    def test_score_text(self) -> None:
        self.assertEqual(validate_score_text("  Тема  ", " описание "), ("Тема", "описание"))
        with self.assertRaises(ValidationError):
            validate_score_text("a", "")
        with self.assertRaises(ValidationError):
            validate_score_text("Тема", "x" * 4001)

    def test_cancel_reason(self) -> None:
        self.assertEqual(validate_cancel_reason("  причина "), "причина")
        with self.assertRaises(ValidationError):
            validate_cancel_reason("x" * 201)


if __name__ == "__main__":
    unittest.main()
