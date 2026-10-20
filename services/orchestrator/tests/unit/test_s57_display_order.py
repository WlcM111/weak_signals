"""Порядок карточек в выдаче: по индексу уверенности по убыванию (устойчиво), либо прежний «модель первой»."""

from __future__ import annotations

import unittest

from orchestrator.application.stages.narrate_rubric import display_order

from .test_rubric_selection import cand, doc


class DisplayOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [cand(i, [doc(i)]) for i in range(1, 6)]
        # как в прогоне s56: первая группа (модель) 0.79…0.62, затем вторая с более высокими индексами
        self.scores = {c.candidate_id: s for c, s in zip(self.items, (0.79, 0.77, 0.62, 0.81, 0.79), strict=True)}

    def ids(self, items: list) -> list[int]:  # noqa: ANN001
        return [self.items.index(c) + 1 for c in items]

    def test_confidence_order_is_descending_and_stable(self) -> None:
        ordered = display_order(self.items, self.scores, "confidence")
        self.assertEqual(self.ids(ordered), [4, 1, 5, 2, 3])  # 0.81, 0.79 (раньше), 0.79, 0.77, 0.62
        values = [self.scores[c.candidate_id] for c in ordered]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_model_first_keeps_previous_order(self) -> None:
        self.assertEqual(self.ids(display_order(self.items, self.scores, "model_first")), [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
