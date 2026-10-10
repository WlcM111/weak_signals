"""Этап 4 (B4): самые близкие к запросу одиночные документы остаются кандидатами."""

from __future__ import annotations

import unittest

from analyzer.domain.clustering import pick_singletons


class SingletonCandidatesTest(unittest.TestCase):
    def test_most_relevant_singletons_within_quarter_of_pool(self) -> None:
        relevances = [0.1, 0.9, 0.5, 0.9, 0.3]
        ids = ["e", "b", "c", "a", "d"]
        self.assertEqual(pick_singletons([0, 1, 2, 3, 4], relevances, ids, 3, 80), [[3], [1], [2]])
        self.assertEqual(pick_singletons([0, 1, 2, 3, 4], relevances, ids, 8, 8), [[3], [1]])  # не больше 8 // 4
        self.assertEqual(pick_singletons([0, 1], relevances, ids, 0, 80), [])


if __name__ == "__main__":
    unittest.main()
