"""U-02: одиночные научные работы, препринты и патенты идут в пул рубрики, как одиночные заметки СМИ."""

from __future__ import annotations

import unittest

from orchestrator.application.stages import rubric
from orchestrator.domain.values import Decision

from .test_rubric_selection import cand, doc


class ResearchSinglesPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.weak = [cand(1, [doc(1), doc(2)])]
        self.news = cand(2, [doc(3, "NEWS")], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE", rank=0, score=0.6)
        self.paper = cand(3, [doc(4, "SCIENTIFIC_PUBLICATION")], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE",
                          rank=0, score=0.7)
        self.preprint = cand(4, [doc(5, "PREPRINT")], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE", rank=0, score=0.5)
        self.patent = cand(5, [doc(6, "PATENT")], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE", rank=0, score=0.4)
        self.code = cand(6, [doc(7, "CODE_REPOSITORY")], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE", rank=0, score=0.9)
        self.excluded = [self.news, self.paper, self.preprint, self.patent, self.code]

    def ids(self, items: list) -> list[str]:  # noqa: ANN001
        return [item.candidate_id for item in items]

    def test_research_singles_join_the_pool_by_score(self) -> None:
        pool, rest = rubric.pool_candidates(self.weak, self.excluded, limit=40, research_singles=True)
        expected = [self.weak[0], self.paper, self.news, self.preprint, self.patent]
        self.assertEqual(self.ids(pool), self.ids(expected))
        self.assertEqual(self.ids(rest), [self.code.candidate_id])  # одиночный репозиторий кода — не исследование

    def test_previous_behaviour_without_the_setting(self) -> None:
        pool, _ = rubric.pool_candidates(self.weak, self.excluded, limit=40)
        self.assertEqual(self.ids(pool), self.ids([self.weak[0], self.news]))

    def test_limit_is_respected(self) -> None:
        pool, rest = rubric.pool_candidates(self.weak, self.excluded, limit=3, research_singles=True)
        self.assertEqual(len(pool), 3)
        self.assertEqual(len(pool) + len(rest), 1 + len(self.excluded))


if __name__ == "__main__":
    unittest.main()
