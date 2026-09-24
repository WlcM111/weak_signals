"""Доверенность источников: отбор кандидатов с весами и требование источника высокой доверенности."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from analyzer.application.use_cases.run_analysis import RunAnalysisConfig
from analyzer.domain.clustering import TRUST_WEIGHTS, select_top_clusters, trust_weights
from analyzer.domain.rules import RuleThresholds, apply_exclusion_rules
from analyzer.domain.values import Decision, DecisionReason, OperationStatus, SourceType, TrustLevel

from ..fakes import make_document
from .test_rules import features
from .test_run_analysis import NOW, RunAnalysisHarness, emerging_documents

REQUIRE = replace(RuleThresholds(), require_high_trust=True)


def repository_documents() -> list:
    """Кластер ранней стадии, подтверждённый только репозиториями кода (средняя доверенность)."""
    return [
        replace(
            document,
            source_type=SourceType.CODE_REPOSITORY,
            trust_level=TrustLevel.MEDIUM,
            published_at=NOW - timedelta(days=20 * index),
        )
        for index, document in enumerate(emerging_documents(), 1)
    ]


class TrustWeightedSelectionTest(unittest.TestCase):
    """Отбор кандидатов: без весов — прежняя формула, с весами — доверенность важнее размера."""

    CLUSTERS = [[0, 1, 2], [3, 4]]
    RELEVANCES = [0.9, 0.9, 0.9, 0.9, 0.9]
    LEVELS = [TrustLevel.MEDIUM] * 3 + [TrustLevel.HIGH] * 2

    def test_without_weights_larger_cluster_wins(self) -> None:
        self.assertEqual(select_top_clusters(self.CLUSTERS, self.RELEVANCES, max_candidates=1), [[0, 1, 2]])

    def test_with_weights_high_trust_cluster_wins(self) -> None:
        documents = [make_document(i + 1, f"Документ {i}", trust_level=level) for i, level in enumerate(self.LEVELS)]
        selected = select_top_clusters(self.CLUSTERS, self.RELEVANCES, 1, trust_weights(documents))
        self.assertEqual(selected, [[3, 4]])

    def test_uniform_weights_keep_previous_order(self) -> None:
        clusters = [[0, 1, 2], [3], [4, 5]]
        relevances = [0.9, 0.9, 0.9, 0.99, 0.5, 0.5]
        unweighted = select_top_clusters(clusters, relevances, max_candidates=2)
        uniform = select_top_clusters(clusters, relevances, 2, [1.0] * len(relevances))
        self.assertEqual(unweighted, uniform)

    def test_weights_by_trust_level(self) -> None:
        documents = [make_document(i + 1, "Документ", trust_level=level) for i, level in enumerate(TrustLevel)]
        self.assertEqual(trust_weights(documents), [TRUST_WEIGHTS[level] for level in TrustLevel])
        self.assertGreater(TRUST_WEIGHTS[TrustLevel.HIGH], TRUST_WEIGHTS[TrustLevel.MEDIUM])
        self.assertGreater(TRUST_WEIGHTS[TrustLevel.MEDIUM], TRUST_WEIGHTS[TrustLevel.LOW])


class HighTrustRuleTest(unittest.TestCase):
    """Правило «нет источника высокой доверенности» работает только при включённом требовании."""

    MEDIUM_ONLY = [make_document(i, f"Репозиторий {i}", trust_level=TrustLevel.MEDIUM) for i in range(1, 4)]

    def test_disabled_by_default(self) -> None:
        self.assertFalse(RuleThresholds().require_high_trust)
        self.assertIsNone(apply_exclusion_rules(features(), self.MEDIUM_ONLY, RuleThresholds()))

    def test_medium_only_excluded_when_required(self) -> None:
        result = apply_exclusion_rules(features(), self.MEDIUM_ONLY, REQUIRE)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIs(result.decision, Decision.INSUFFICIENT_EVIDENCE)
        self.assertIs(result.reason, DecisionReason.NO_TRUSTED_SOURCE)
        self.assertIn("высокой доверенности", result.explanation_ru)
        self.assertIn("3 источников", result.explanation_ru)

    def test_one_high_trust_source_is_enough(self) -> None:
        documents = [*self.MEDIUM_ONLY, make_document(9, "Статья", trust_level=TrustLevel.HIGH)]
        self.assertIsNone(apply_exclusion_rules(features(), documents, REQUIRE))

    def test_not_applicable_without_documents(self) -> None:
        self.assertIsNone(apply_exclusion_rules(features(), [], REQUIRE))

    def test_off_topic_still_checked_first(self) -> None:
        result = apply_exclusion_rules(features(emb_sim_query=0.0), self.MEDIUM_ONLY, REQUIRE)
        assert result is not None
        self.assertIs(result.decision, Decision.OFF_TOPIC)

    def test_threshold_from_rules_file(self) -> None:
        self.assertTrue(RuleThresholds.from_mapping({"require_high_trust": True}).require_high_trust)


class ConfigWiringTest(RunAnalysisHarness):
    """Флаги доходят от конфигурации сценария до решения по кандидату."""

    def run_with(self, documents: list, **flags: bool) -> list:
        config = RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2, **flags)
        use_case, analysis, _analyses, candidates, _reader = self.build(documents, config=config)
        self.assertIs(use_case.execute(analysis, "worker-1"), OperationStatus.COMPLETED)
        return candidates.saved[analysis.analysis_id]

    def test_flags_are_off_by_default(self) -> None:
        config = RunAnalysisConfig()
        self.assertFalse(config.trust_weighted_selection)
        self.assertFalse(config.require_high_trust_source)

    def test_repository_only_cluster_excluded_when_required(self) -> None:
        saved = self.run_with(repository_documents(), require_high_trust_source=True)
        self.assertEqual(len(saved), 1)
        self.assertIs(saved[0].decision, Decision.INSUFFICIENT_EVIDENCE)
        self.assertIs(saved[0].decision_reason, DecisionReason.NO_TRUSTED_SOURCE)

    def test_repository_only_cluster_not_excluded_by_default(self) -> None:
        saved = self.run_with(repository_documents())
        self.assertEqual(len(saved), 1)
        self.assertIsNot(saved[0].decision_reason, DecisionReason.NO_TRUSTED_SOURCE)

    def test_high_trust_cluster_passes_when_required(self) -> None:
        saved = self.run_with(emerging_documents(), require_high_trust_source=True)
        self.assertEqual(len(saved), 1)
        self.assertIs(saved[0].decision, Decision.WEAK_SIGNAL)

    def test_trust_weighted_selection_runs_end_to_end(self) -> None:
        default = self.run_with(emerging_documents() + repository_documents())
        weighted = self.run_with(emerging_documents() + repository_documents(), trust_weighted_selection=True)
        self.assertEqual(len(default), len(weighted))


if __name__ == "__main__":
    unittest.main()