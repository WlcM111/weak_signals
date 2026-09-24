"""Восемь правил исключения, решение по скорингу, объяснения и ранжирование (§12.6, §12.5 ТЗ)."""

from __future__ import annotations

import math
import unittest

from analyzer.domain.entities import Candidate, FeatureContribution
from analyzer.domain.rules import (
    RuleThresholds,
    apply_exclusion_rules,
    decide_by_score,
    direction_of,
    explain_model_decision,
    rank_weak_signals,
)
from analyzer.domain.values import Decision, DecisionReason, FeatureDirection, TrustLevel
from ..fakes import make_document

THRESHOLDS = RuleThresholds()
NEUTRAL = {
    "emb_sim_query": 0.8,
    "wiki_exists": 0.0,
    "wiki_pageviews_30d_log": 0.0,
    "wiki_age_years": 0.0,
    "stage_lex_ordinal": 2.0,
    "bigtech_mentions_count": 0.0,
    "share_code_vacancy": 0.0,
    "lex_maturity_score": 0.1,
    "lex_emergence_score": 0.5,
    "share_marketing": 0.1,
    "share_scientific": 0.4,
    "lex_hype_score": 0.1,
    "trusted_share": 0.6,
}
DOCS = [
    make_document(1, "A", trust_level=TrustLevel.HIGH),
    make_document(2, "B", trust_level=TrustLevel.MEDIUM),
]


def features(**overrides: float) -> dict[str, float]:
    """Нейтральный вектор признаков с точечными изменениями."""
    return {**NEUTRAL, **overrides}


class NoRuleTest(unittest.TestCase):
    """На нейтральных значениях решение принимает модель."""

    def test_returns_none(self) -> None:
        self.assertIsNone(apply_exclusion_rules(features(), DOCS, THRESHOLDS))


class RuleTableTest(unittest.TestCase):
    """Таблица срабатываний: условие → решение и причина."""

    CASES = (
        ("off_topic", {"emb_sim_query": 0.29}, DOCS, Decision.OFF_TOPIC,
         DecisionReason.LOW_QUERY_RELEVANCE),
        ("no_trusted", {}, [make_document(3, "C", trust_level=TrustLevel.LOW)] * 3,
         Decision.INSUFFICIENT_EVIDENCE, DecisionReason.NO_TRUSTED_SOURCE),
        ("single_source", {}, [make_document(4, "D", trust_level=TrustLevel.MEDIUM)],
         Decision.INSUFFICIENT_EVIDENCE, DecisionReason.SINGLE_SOURCE),
        ("encyclopedia", {"wiki_exists": 1.0, "wiki_pageviews_30d_log": math.log1p(25000),
                          "wiki_age_years": 5.0}, DOCS, Decision.MATURE,
         DecisionReason.ENCYCLOPEDIA_MATURE),
        ("market_leaders", {"stage_lex_ordinal": 5.0, "bigtech_mentions_count": 3.0}, DOCS,
         Decision.MATURE, DecisionReason.MARKET_LEADERS),
        ("market_leaders_by_vacancy", {"stage_lex_ordinal": 5.0, "share_code_vacancy": 0.4}, DOCS,
         Decision.MATURE, DecisionReason.MARKET_LEADERS),
        ("maturity_lexicon", {"lex_maturity_score": 0.6, "lex_emergence_score": 0.2}, DOCS,
         Decision.MATURE, DecisionReason.MATURITY_LEXICON),
        ("marketing_dominant", {"share_marketing": 0.6, "share_scientific": 0.0}, DOCS,
         Decision.HYPE_OR_NOISE, DecisionReason.MARKETING_DOMINANT),
        ("hype_lexicon", {"lex_hype_score": 0.5, "trusted_share": 0.2}, DOCS,
         Decision.HYPE_OR_NOISE, DecisionReason.HYPE_LEXICON),
    )

    def test_rules_fire_as_specified(self) -> None:
        for name, overrides, documents, decision, reason in self.CASES:
            with self.subTest(rule=name):
                result = apply_exclusion_rules(features(**overrides), documents, THRESHOLDS)
                self.assertIsNotNone(result, f"правило {name} не сработало")
                self.assertIs(result.decision, decision)
                self.assertIs(result.reason, reason)
                self.assertTrue(result.explanation_ru)
                self.assertLessEqual(len(result.explanation_ru), 500)


class RuleBoundaryTest(unittest.TestCase):
    """Границы порогов: строгие и нестрогие неравенства."""

    def test_query_similarity_at_threshold_does_not_fire(self) -> None:
        self.assertIsNone(apply_exclusion_rules(features(emb_sim_query=0.30), DOCS, THRESHOLDS))

    def test_encyclopedia_needs_all_three_conditions(self) -> None:
        almost = features(
            wiki_exists=1.0, wiki_pageviews_30d_log=math.log1p(19999), wiki_age_years=5.0
        )
        self.assertIsNone(apply_exclusion_rules(almost, DOCS, THRESHOLDS))
        young = features(
            wiki_exists=1.0, wiki_pageviews_30d_log=math.log1p(25000), wiki_age_years=2.9
        )
        self.assertIsNone(apply_exclusion_rules(young, DOCS, THRESHOLDS))

    def test_market_leaders_requires_stage_five(self) -> None:
        values = features(stage_lex_ordinal=4.0, bigtech_mentions_count=10.0)
        self.assertIsNone(apply_exclusion_rules(values, DOCS, THRESHOLDS))

    def test_maturity_lexicon_blocked_by_emergence(self) -> None:
        values = features(lex_maturity_score=0.9, lex_emergence_score=0.21)
        self.assertIsNone(apply_exclusion_rules(values, DOCS, THRESHOLDS))

    def test_marketing_rule_blocked_by_any_science(self) -> None:
        values = features(share_marketing=0.9, share_scientific=0.01)
        self.assertIsNone(apply_exclusion_rules(values, DOCS, THRESHOLDS))

    def test_hype_rule_blocked_by_trusted_sources(self) -> None:
        values = features(lex_hype_score=0.9, trusted_share=0.21)
        self.assertIsNone(apply_exclusion_rules(values, DOCS, THRESHOLDS))


class RuleOrderTest(unittest.TestCase):
    """Правила применяются в фиксированном порядке: побеждает первое подходящее."""

    def test_off_topic_wins_over_maturity(self) -> None:
        values = features(emb_sim_query=0.1, lex_maturity_score=0.9, lex_emergence_score=0.0)
        result = apply_exclusion_rules(values, DOCS, THRESHOLDS)
        self.assertIs(result.reason, DecisionReason.LOW_QUERY_RELEVANCE)

    def test_no_trusted_source_wins_over_hype(self) -> None:
        low_documents = [make_document(index, "x", trust_level=TrustLevel.LOW) for index in range(3)]
        values = features(lex_hype_score=0.9, trusted_share=0.0)
        result = apply_exclusion_rules(values, low_documents, THRESHOLDS)
        self.assertIs(result.reason, DecisionReason.NO_TRUSTED_SOURCE)


class SingleSourceRuleTest(unittest.TestCase):
    """Правило 3: обобщение параметром `min_evidence_documents`."""

    def test_high_trust_single_document_is_not_excluded(self) -> None:
        documents = [make_document(5, "E", trust_level=TrustLevel.HIGH)]
        self.assertIsNone(apply_exclusion_rules(features(), documents, THRESHOLDS))

    def test_requires_more_documents_when_asked(self) -> None:
        documents = [make_document(index, "x", trust_level=TrustLevel.MEDIUM) for index in range(2)]
        result = apply_exclusion_rules(features(), documents, THRESHOLDS, min_evidence_documents=3)
        self.assertIs(result.reason, DecisionReason.SINGLE_SOURCE)

    def test_not_applicable_without_collection(self) -> None:
        self.assertIsNone(apply_exclusion_rules(features(), [], THRESHOLDS))


class ThresholdsFromManifestTest(unittest.TestCase):
    """Пороги берутся из `rules.json`, неизвестные ключи игнорируются."""

    def test_overrides_known_keys(self) -> None:
        thresholds = RuleThresholds.from_mapping({"min_query_similarity": 0.5, "unknown": 1})
        self.assertEqual(thresholds.min_query_similarity, 0.5)
        self.assertEqual(thresholds.maturity_min, RuleThresholds().maturity_min)

    def test_empty_mapping_gives_defaults(self) -> None:
        self.assertEqual(RuleThresholds.from_mapping(None), RuleThresholds())

    def test_custom_threshold_changes_rule(self) -> None:
        thresholds = RuleThresholds.from_mapping({"min_query_similarity": 0.9})
        result = apply_exclusion_rules(features(emb_sim_query=0.85), DOCS, thresholds)
        self.assertIs(result.decision, Decision.OFF_TOPIC)


class DecideByScoreTest(unittest.TestCase):
    """Решение по калиброванной вероятности (§12.5, шаг 9)."""

    def test_weak_signal_at_threshold(self) -> None:
        self.assertIs(decide_by_score(0.5, 0.5, features(), THRESHOLDS), Decision.WEAK_SIGNAL)

    def test_mature_by_lexicon(self) -> None:
        values = features(lex_maturity_score=0.4)
        self.assertIs(decide_by_score(0.3, 0.5, values, THRESHOLDS), Decision.MATURE)

    def test_mature_by_wikipedia(self) -> None:
        values = features(lex_maturity_score=0.0, wiki_exists=1.0)
        self.assertIs(decide_by_score(0.3, 0.5, values, THRESHOLDS), Decision.MATURE)

    def test_hype_by_lexicon(self) -> None:
        values = features(lex_maturity_score=0.0, lex_hype_score=0.3)
        self.assertIs(decide_by_score(0.3, 0.5, values, THRESHOLDS), Decision.HYPE_OR_NOISE)

    def test_insufficient_evidence_otherwise(self) -> None:
        values = features(lex_maturity_score=0.0, lex_hype_score=0.0)
        self.assertIs(decide_by_score(0.3, 0.5, values, THRESHOLDS), Decision.INSUFFICIENT_EVIDENCE)


class ExplanationTest(unittest.TestCase):
    """Объяснение модели: русский текст, порог и два главных вклада."""

    CONTRIBUTIONS = (
        FeatureContribution("lex_emergence_score", 0.8, 0.41, "Лексика ранней стадии",
                            FeatureDirection.SUPPORTS_WEAK_SIGNAL),
        FeatureContribution("share_scientific", 0.5, -0.22, "Доля научных публикаций",
                            FeatureDirection.SUPPORTS_MATURE),
        FeatureContribution("doc_count_log", 2.0, 0.01, "Объём упоминаний",
                            FeatureDirection.NEUTRAL),
    )

    def test_mentions_threshold_and_top_features(self) -> None:
        text = explain_model_decision(0.83, 0.62, self.CONTRIBUTIONS)
        self.assertIn("0.83", text)
        self.assertIn("0.62", text)
        self.assertIn("лексика ранней стадии", text)
        self.assertIn("+0.41", text)
        self.assertNotIn("doc_count_log", text)
        self.assertLessEqual(len(text), 500)

    def test_below_threshold_wording(self) -> None:
        self.assertIn("ниже порога", explain_model_decision(0.2, 0.6, self.CONTRIBUTIONS))

    def test_at_threshold_wording(self) -> None:
        self.assertIn("не ниже порога", explain_model_decision(0.6, 0.6, self.CONTRIBUTIONS))

    def test_without_contributions(self) -> None:
        self.assertTrue(explain_model_decision(0.5, 0.5, ()))


class DirectionTest(unittest.TestCase):
    """Полоса нейтральности ±0.05 логита."""

    def test_directions(self) -> None:
        self.assertIs(direction_of(0.06), FeatureDirection.SUPPORTS_WEAK_SIGNAL)
        self.assertIs(direction_of(-0.06), FeatureDirection.SUPPORTS_MATURE)
        self.assertIs(direction_of(0.05), FeatureDirection.NEUTRAL)
        self.assertIs(direction_of(-0.05), FeatureDirection.NEUTRAL)


class RankingTest(unittest.TestCase):
    """Ранжирование: score ↓, trusted_share ↓, query_relevance ↓."""

    @staticmethod
    def candidate(index: int, score: float, trusted: float, relevance: float) -> Candidate:
        """Кандидат-слабый сигнал с заданными значениями тай-брейков."""
        return Candidate(
            cluster_index=index,
            title_auto=f"кандидат {index}",
            keyphrases=("фраза",),
            score=score,
            decision=Decision.WEAK_SIGNAL,
            decision_reason=DecisionReason.MODEL_SCORE,
            decision_explanation_ru="объяснение",
            features=(
                FeatureContribution("trusted_share", trusted, 0.0, "Доля доверенных",
                                    FeatureDirection.NEUTRAL),
            ),
            documents=(),
            document_count=3,
            query_relevance=relevance,
            rank=1,
        )

    def test_orders_by_score(self) -> None:
        items = [self.candidate(1, 0.5, 0.5, 0.5), self.candidate(2, 0.9, 0.1, 0.1)]
        ranked = rank_weak_signals(items)
        self.assertEqual([item.cluster_index for item in ranked], [2, 1])
        self.assertEqual([item.rank for item in ranked], [1, 2])

    def test_tie_break_by_trusted_share(self) -> None:
        items = [self.candidate(1, 0.8, 0.2, 0.9), self.candidate(2, 0.8, 0.7, 0.1)]
        self.assertEqual([item.cluster_index for item in rank_weak_signals(items)], [2, 1])

    def test_tie_break_by_query_relevance(self) -> None:
        items = [self.candidate(1, 0.8, 0.5, 0.4), self.candidate(2, 0.8, 0.5, 0.6)]
        self.assertEqual([item.cluster_index for item in rank_weak_signals(items)], [2, 1])

    def test_excluded_candidates_keep_zero_rank(self) -> None:
        weak = self.candidate(1, 0.9, 0.5, 0.5)
        excluded = Candidate(
            cluster_index=2,
            title_auto="исключён",
            keyphrases=("фраза",),
            score=0.0,
            decision=Decision.MATURE,
            decision_reason=DecisionReason.MATURITY_LEXICON,
            decision_explanation_ru="объяснение",
            features=(),
            documents=(),
            document_count=2,
            query_relevance=0.5,
        )
        ranked = rank_weak_signals([weak, excluded])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(excluded.rank, 0)


if __name__ == "__main__":
    unittest.main()
