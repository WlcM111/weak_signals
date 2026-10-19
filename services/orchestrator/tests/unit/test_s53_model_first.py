"""Этап 5.3: кандидаты с лучшей оценкой модели analyzer ставятся в выдаче первыми; ничего не удаляется."""

from __future__ import annotations

import unittest

from orchestrator.application.stages import rubric

from .test_rubric_selection import cand, doc


class ModelPreferredTest(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [cand(i, [doc(i)], score=s) for i, s in enumerate([0.9, 0.2, 0.95, 0.4, 0.1, 0.8], 1)]

    def test_top_share_by_analyzer_score(self) -> None:
        preferred = rubric.model_preferred(self.items, 0.5)
        self.assertEqual(preferred, {self.items[2].candidate_id, self.items[0].candidate_id, self.items[5].candidate_id})

    def test_zero_share_keeps_previous_order(self) -> None:
        self.assertEqual(rubric.model_preferred(self.items, 0.0), set())
        self.assertEqual(rubric.model_preferred([], 0.5), set())

    def test_odd_count_rounds_up_and_ties_are_stable(self) -> None:
        tied = [cand(i, [doc(i)], score=0.5) for i in range(1, 6)]
        first = rubric.model_preferred(tied, 0.5)
        self.assertEqual(len(first), 3)
        self.assertEqual(first, rubric.model_preferred(list(reversed(tied)), 0.5))


if __name__ == "__main__":
    unittest.main()
