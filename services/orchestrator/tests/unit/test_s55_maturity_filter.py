"""Этап 5.5: локальная модель зрелости после доводки — проверяются только карточки с научными источниками;
сбой analyzer и отсутствие модели не исключают ничего; текст карточки собирается так же, как при обучении."""

from __future__ import annotations

import unittest

from orchestrator.application.dto import DocumentView, FinalizedCardView
from orchestrator.application.stages.narrate_rubric import _maturity_filter, maturity_card_text

from ..fakes import FakeAnalyzer
from .test_rubric_selection import cand, doc


def card(cid: str, title: str, doc_ids: tuple[str, ...]) -> FinalizedCardView:
    return FinalizedCardView(candidate_id=cid, title_ru=title, description_ru="Описание.", advantage_ru="",
                             case_example_ru="", case_document_id="", why_ru="", companies=(), stage=2, trend=2,
                             stage_reason_ru="", trend_reason_ru="", source_summaries={d: ("", "") for d in doc_ids})


class MaturityFilterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.paper, self.news = doc(1, "SCIENTIFIC_PUBLICATION"), doc(2, "NEWS")
        self.documents: dict[str, DocumentView] = {self.paper.document_id: self.paper, self.news.document_id: self.news}
        self.batch = [cand(1, [self.paper]), cand(2, [self.paper]), cand(3, [self.paper, self.news])]
        self.finalized = {
            self.batch[0].candidate_id: card(self.batch[0].candidate_id, "Зрелая задача", (self.paper.document_id,)),
            self.batch[1].candidate_id: card(self.batch[1].candidate_id, "Новый механизм", (self.paper.document_id,)),
            self.batch[2].candidate_id: card(self.batch[2].candidate_id, "Зрелая задача стартапа",
                                             (self.paper.document_id, self.news.document_id)),
        }

    async def test_only_research_cards_are_scored_and_threshold_applies(self) -> None:
        analyzer = FakeAnalyzer()
        analyzer.maturity_scores = {"Зрелая задача": 0.9}
        flagged = await _maturity_filter(analyzer, self.batch, self.finalized, self.documents)
        self.assertEqual(set(flagged), {self.batch[0].candidate_id})
        self.assertEqual(flagged[self.batch[0].candidate_id][1:], (0.7, "test-v1"))
        self.assertEqual(len(analyzer.maturity_calls[0]), 2)  # карточка с новостью в модель не отправляется

    async def test_no_model_and_failure_keep_cards(self) -> None:
        analyzer = FakeAnalyzer()
        self.assertEqual(await _maturity_filter(analyzer, self.batch, self.finalized, self.documents), {})
        analyzer.maturity_scores = {"Зрелая задача": 0.9}
        analyzer.maturity_error = RuntimeError("analyzer недоступен")
        self.assertEqual(await _maturity_filter(analyzer, self.batch, self.finalized, self.documents), {})

    def test_card_text_matches_training(self) -> None:
        self.assertEqual(maturity_card_text("Т", "О" * 700, ["a" * 200, "b", "c", "d"]),
                         f"Т. {'О' * 600} Источники: {'a' * 120}; b; c")


if __name__ == "__main__":
    unittest.main()
