"""U-04: смешанный предотбор — квоты модели и близости, без повторов, детерминированный, ровно min(limit, n)."""

from __future__ import annotations

import unittest

from analyzer.application.use_cases.run_analysis import select_mixed


class MixedSelectionTest(unittest.TestCase):
    def test_half_by_model_half_by_relevance(self) -> None:
        model = [0.9, 0.8, 0.7, 0.1, 0.2, 0.3]
        relevance = [0.1, 0.2, 0.3, 0.9, 0.8, 0.7]
        self.assertEqual(select_mixed(model, relevance, 4, 0.5, [False] * 6, 0), [0, 1, 3, 4])

    def test_overlap_is_refilled_and_size_is_exact(self) -> None:
        model = [0.9, 0.8, 0.7, 0.6]
        relevance = [0.9, 0.8, 0.7, 0.6]  # рейтинги совпадают
        self.assertEqual(select_mixed(model, relevance, 3, 0.5, [False] * 4, 0), [0, 1, 2])
        self.assertEqual(select_mixed(model, relevance, 10, 0.5, [False] * 4, 0), [0, 1, 2, 3])  # n < limit
        self.assertEqual(select_mixed([], [], 5, 0.5, [], 0), [])

    def test_saturated_model_scores_break_ties_by_relevance(self) -> None:
        model = [1.0, 1.0, 1.0, 1.0]
        relevance = [0.2, 0.9, 0.5, 0.8]
        self.assertEqual(select_mixed(model, relevance, 2, 1.0, [False] * 4, 0), [1, 3])  # не позиционный порядок

    def test_odd_limit_and_market_reserve(self) -> None:
        model = [0.9, 0.1, 0.8, 0.2, 0.7]
        relevance = [0.1, 0.9, 0.2, 0.8, 0.3]
        market = [False, False, False, False, True]
        picked = select_mixed(model, relevance, 3, 0.5, market, 1)
        self.assertEqual(len(picked), 3)
        self.assertIn(4, picked)  # резерв заметок СМИ
        self.assertEqual(picked, select_mixed(model, relevance, 3, 0.5, market, 1))  # детерминированность


if __name__ == "__main__":
    unittest.main()
