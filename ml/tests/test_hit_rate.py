"""Попадание в сигналы организаторов: сопоставление и подсчёт без модели эмбеддингов."""

from __future__ import annotations

import unittest

import numpy as np

from ml import hit_rate


class HitRateTest(unittest.TestCase):
    def test_match_and_summarize(self) -> None:
        orgs = np.array([[1.0, 0.0], [0.0, 1.0]])
        cards = np.array([[0.9, 0.436], [0.0, 1.0]])
        best, index = hit_rate.match(orgs, cards)
        self.assertEqual(index.tolist(), [0, 1])
        report = hit_rate.summarize({"edge": best, "fintech": np.array([0.5])}, {"edge": 2, "fintech": 0})
        self.assertEqual(report["by_domain"]["edge"]["hits@0.89"], 2)
        self.assertEqual(report["total"]["hits@0.89"], {"hits": 2, "of": 3, "share": 0.6667})

    def test_no_cards(self) -> None:
        best, index = hit_rate.match(np.array([[1.0, 0.0]]), np.zeros((0, 2)))
        self.assertEqual((best.tolist(), index.tolist()), ([0.0], [-1]))


if __name__ == "__main__":
    unittest.main()
