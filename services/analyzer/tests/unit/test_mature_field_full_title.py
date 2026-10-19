"""R-023 (ANA-09): правило 6б и признак Wikipedia — по полному названию кластера, без вложенных n-грамм (за флагом)."""

from __future__ import annotations

import types
import unittest
from datetime import timedelta

from analyzer.application.dto import EncyclopediaHit
from analyzer.application.use_cases.run_analysis import RunAnalysisConfig, _ClusterView
from analyzer.domain import keyphrases as phrases
from analyzer.domain.entities import Candidate
from analyzer.domain.values import Decision, DecisionReason

from ..fakes import NOW, FakeCollector
from .test_run_analysis import RunAnalysisHarness, emerging_documents

NEW_APPLICATION = ("federated anomaly detection for wind turbines",
                   ("federated anomaly detection", "anomaly detection", "wind turbines"))
NESTED = ("federated anomaly detection", "anomaly detection", "edge sensors")  # общая n-грамма вложена в первую


def _popular(title: str) -> dict[str, EncyclopediaHit]:
    """Давняя посещаемая статья Wikipedia (порог правила 4 — 20000 просмотров и 3 года)."""
    hit = EncyclopediaHit(title=title, exists=True, pageviews_30d=90000, created_at=NOW - timedelta(days=4383))
    return {title.lower(): hit}


class MatureFieldFullTitleTest(RunAnalysisHarness):
    """Кластер о новом применении зрелой области не исключается из-за общей n-граммы в ключевых фразах."""

    def _decide(
        self, title: str, keyphrases: tuple[str, ...], flag: bool | None,
        encyclopedia: dict[str, EncyclopediaHit] | None = None,
    ) -> tuple[Candidate, dict[str, float]]:
        """Кандидат и признаки кластера из документов ранней стадии с заданными названием и ключевыми фразами."""
        documents = emerging_documents()
        options = {} if flag is None else {"mature_field_full_title": flag}  # None — значение по умолчанию
        config = RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2, encyclopedia_languages=("en",),
                                   **options)
        collector = FakeCollector(documents=documents, encyclopedia=encyclopedia or {})
        use_case, analysis, _, _, _ = self.build(documents, config=config, collector=collector)
        vectors = self.embedder.encode([document.title for document in documents], "passage: ")
        view = _ClusterView(cluster_index=0, documents=documents, similarities=[1.0] * len(documents),
                            centroid=vectors.mean(axis=0), title_auto=title, keyphrases=keyphrases,
                            query_relevance=0.9)
        control = types.SimpleNamespace(check=lambda: None)
        signals = use_case._encyclopedia_signals([view], control)
        view.values = use_case._feature_values(view, signals.get(0), self.bundle, NOW)
        [candidate] = use_case._decide([view], self.bundle, analysis, control)
        return candidate, view.values

    def test_new_application_is_not_mature_field_with_flag(self) -> None:
        candidate, _ = self._decide(*NEW_APPLICATION, flag=True)
        self.assertIsNot(candidate.decision, Decision.MATURE, candidate.decision_explanation_ru)
        self.assertIs(candidate.decision_reason, DecisionReason.MODEL_SCORE)  # решает модель, не правило 6б

    def test_bare_mature_field_title_is_still_mature_with_flag(self) -> None:
        candidate, _ = self._decide("AI anomaly detection", ("AI anomaly detection", "anomaly detection"), flag=True)
        self.assertIs(candidate.decision, Decision.MATURE)
        self.assertIs(candidate.decision_reason, DecisionReason.MATURITY_LEXICON)
        self.assertIn("anomaly detection", candidate.decision_explanation_ru)

    def test_nested_generic_ngram_gives_no_wiki_with_flag(self) -> None:
        candidate, values = self._decide("federated anomaly detection", NESTED, flag=True,
                                         encyclopedia=_popular("anomaly detection"))
        self.assertEqual(values["wiki_exists"], 0.0)
        self.assertIsNot(candidate.decision, Decision.MATURE, candidate.decision_explanation_ru)
        # полная ключевая фраза по-прежнему проверяется
        _, values = self._decide("federated anomaly detection", NESTED, flag=True,
                                 encyclopedia=_popular("federated anomaly detection"))
        self.assertEqual(values["wiki_exists"], 1.0)

    def test_flag_off_keeps_previous_behaviour(self) -> None:
        self.assertFalse(RunAnalysisConfig().mature_field_full_title)
        for flag in (None, False):
            candidate, _ = self._decide(*NEW_APPLICATION, flag=flag)
            self.assertIs(candidate.decision, Decision.MATURE)
            self.assertIs(candidate.decision_reason, DecisionReason.MATURITY_LEXICON)
            candidate, values = self._decide("federated anomaly detection", NESTED, flag=flag,
                                             encyclopedia=_popular("anomaly detection"))
            self.assertEqual(values["wiki_exists"], 1.0)
            self.assertIs(candidate.decision_reason, DecisionReason.ENCYCLOPEDIA_MATURE)


class WithoutNestedTest(unittest.TestCase):
    def test_nested_ngrams_are_dropped_by_words(self) -> None:
        self.assertEqual(phrases.without_nested(["federated anomaly detection", "anomaly detection", "edge sensors"]),
                         ["federated anomaly detection", "edge sensors"])
        self.assertEqual(phrases.without_nested(["Anomaly Detection", "Federated anomaly detection: a survey"]),
                         ["Federated anomaly detection: a survey"])
        self.assertEqual(phrases.without_nested(["genai security", "ai security"]), ["genai security", "ai security"])


if __name__ == "__main__":
    unittest.main()
