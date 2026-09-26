"""Инструмент оценки рубричного судья: построение кандидатов из A и B v2 и метрики (без модели и БД)."""

from __future__ import annotations

import unittest

from insight import eval_rubric as ev
from insight.application.use_cases.rubric_judge import RubricVerdict

A_POS = {"row_id": "pos-1", "title": "Агентный IAM", "description": "Раунд $8M, выход из stealth.", "label": 1,
         "domain_tag": "ai_security", "source_urls": ["https://siliconangle.com/x", "https://arxiv.org/abs/1"],
         "stage_ordinal": 4, "trend_ordinal": 3}
A_NEG = {"row_id": "neg-1", "title": "Предиктивное обслуживание", "description": "Входит в пакеты SCADA.", "label": 0,
         "domain_tag": "industrial_ai", "source_urls": ["https://en.wikipedia.org/wiki/Predictive_maintenance"],
         "stage_ordinal": None, "trend_ordinal": None}


def verdict(cid: str, code: str, stage: int = 2, trend: int = 2) -> RubricVerdict:
    return RubricVerdict(cid, code, True, True, True, code == "R", stage, trend, 3, 0.8, "Причина.")


class EvalRubricTest(unittest.TestCase):
    def test_a_item_types_and_snippet(self) -> None:
        item = ev.a_item(A_POS)
        self.assertEqual([(s.domain, s.source_type, s.trust_level) for s in item.sources],
                         [("siliconangle.com", "INDUSTRY_MEDIA", "MEDIUM"), ("arxiv.org", "PREPRINT", "HIGH")])
        self.assertEqual(item.sources[0].snippet, A_POS["description"])
        self.assertEqual(ev.a_item(A_NEG).sources[0].source_type, "ENCYCLOPEDIA")

    def test_grouped_by_case_query(self) -> None:
        groups = ev.grouped("a", [A_POS, A_NEG], [ev.a_item(A_POS), ev.a_item(A_NEG)])
        self.assertEqual(set(groups), {"защита и безопасность систем искусственного интеллекта",
                                       "индустриальный искусственный интеллект на производстве"})

    def test_metrics(self) -> None:
        self.assertEqual(ev.classification([1, 1, 0, 0], [1, 0, 1, 0])["f1"], 0.5)
        self.assertEqual(ev.qwk([1, 2, 3, 4], [1, 2, 3, 4], 1, 4), 1.0)
        self.assertLess(ev.qwk([1, 2, 3, 4], [4, 3, 2, 1], 1, 4), 0)
        self.assertEqual(ev.spearman([1, 2, 3], [10, 20, 30]), 1.0)

    def test_summarize_a(self) -> None:
        metrics, predictions = ev.summarize("a", [A_POS, A_NEG], {"pos-1": verdict("pos-1", "R", 4, 2),
                                                                   "neg-1": verdict("neg-1", "N-MAT")})
        self.assertEqual(metrics["filter"]["accuracy"], 1.0)
        self.assertEqual((metrics["stage"]["accuracy"], metrics["trend"]["mae"]), (1.0, 1.0))
        self.assertEqual(predictions[0]["pred_code"], "R")

    def test_b_item_uses_evidence(self) -> None:
        row = {"group_id": "bgrp-1", "title": "Лидар", "observation_text": "Текст карточки.", "topic": "лидары",
               "evidence_refs": [{"title": "FMCW LiDAR", "url": "https://www.nature.com/a", "source_type":
                                  "SCIENTIFIC_PUBLICATION", "trust_level": "HIGH", "published_at": "2025-01-02T00:00:00Z",
                                  "language_code": "en", "source_key": "openalex"}]}
        item = ev.b_item(row)
        self.assertEqual((item.sources[0].domain, item.sources[0].published), ("nature.com", "2025-01-02"))


if __name__ == "__main__":
    unittest.main()
