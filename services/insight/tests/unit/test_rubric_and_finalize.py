"""Рубричная оценка и пакетная доводка карточек: сопоставление номеров, проверки по источникам, отказоустойчивость."""

from __future__ import annotations

import json
import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.prompt_builder import FINALIZE_PROMPT_VERSION, RUBRIC_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.finalize_cards import CardInput, FinalizeCards, check_card
from insight.application.use_cases.rubric_judge import RubricItem, RubricJudge, RubricSource, to_verdict

from .test_chain_and_use_cases import PROMPTS, SCHEMAS, build_chain

SOURCES = (
    RubricSource("doc-1", "Chip-scale FMCW LiDAR engine reaches 200 m range", "openalex", "SCIENTIFIC_PUBLICATION",
                 "HIGH", "2025-03-01", "en", "Researchers at EPFL demonstrated a photonic LiDAR engine with 200 m range.",
                 "nature.com"),
    RubricSource("doc-2", "Стартап Lumotive привлёк инвестиции", "rss", "INDUSTRY_MEDIA", "MEDIUM", "2026-01-10", "ru",
                 "Компания Lumotive привлекла 45 млн долларов на пилоты метаповерхностных лидаров.", "rb.ru"),
)
ITEMS = [RubricItem(f"cand-{n}", f"Кандидат {n}", ("lidar",), SOURCES, "2 источника: 1 научная, 1 СМИ") for n in range(1, 11)]


def verdict_row(short: str, code: str = "R", ok: bool = True, stage: int = 2, trend: int = 3) -> dict:
    return {"candidate_id": short, "code": code, "on_topic": ok, "concrete": ok, "early_stage": ok, "verifiable": ok,
            "stage": stage, "trend": trend, "relevance": 3, "confidence": 0.8, "reason_ru": "Причина."}


class RubricJudgeTest(unittest.IsolatedAsyncioTestCase):
    def judge(self, *responses: str, batch: int = 8) -> tuple[RubricJudge, FakeProvider]:
        provider = FakeProvider(responses=list(responses))
        return RubricJudge(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS), batch_size=batch), provider

    async def test_batches_map_short_ids_and_keep_criteria(self) -> None:
        first = json.dumps({"verdicts": [verdict_row(f"c{n}") for n in range(1, 9)]}, ensure_ascii=False)
        second = json.dumps({"verdicts": [verdict_row("c1", "N-OVR", ok=False), verdict_row("c7")]}, ensure_ascii=False)
        judge, provider = self.judge(first, second)
        outcome = await judge.execute("твердотельные лидары", ITEMS)
        self.assertFalse(outcome.used_fallback)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([v.candidate_id for v in outcome.verdicts][-1], "cand-9")  # c7 второй пачки неизвестен
        self.assertEqual(outcome.verdicts[-1].code, "N-OVR")
        self.assertEqual((outcome.verdicts[0].stage, outcome.verdicts[0].trend), (2, 3))
        self.assertIn('Слабый сигнал — code "R"', provider.calls[0][0].content)
        payload = json.loads(provider.calls[0][1].content)
        self.assertEqual(payload["candidates"][0]["sources"][1]["site"], "rb.ru")
        self.assertNotIn("doc-1", provider.calls[0][1].content)
        self.assertEqual(RUBRIC_PROMPT_VERSION, "judge_v2")

    async def test_failed_batch_leaves_others(self) -> None:
        judge, _ = self.judge("не json", json.dumps({"verdicts": [verdict_row("c1")]}))
        outcome = await judge.execute("q", ITEMS)
        self.assertEqual([v.candidate_id for v in outcome.verdicts], ["cand-9"])

    async def test_all_failed_is_fallback(self) -> None:
        judge, _ = self.judge("не json", "тоже не json")
        self.assertTrue((await judge.execute("q", ITEMS)).used_fallback)

    def test_r_without_all_criteria_becomes_u(self) -> None:
        row = verdict_row("c1")
        row["verifiable"] = False
        self.assertEqual(to_verdict("x", row).code, "U")
        self.assertEqual(to_verdict("x", verdict_row("c1")).verdict, "EMERGING_TECHNOLOGY")


def card_row(short: str, **overrides) -> dict:
    row = {"candidate_id": short, "title_ru": "Фотонный FMCW-лидар на чипе",
           "description_ru": "Интегральный фотонный лидар с частотной модуляцией измеряет дальность до 200 м.",
           "advantage_ru": "Компактность и отсутствие движущихся частей.",
           "case_example_ru": "Исследователи EPFL показали фотонный движок лидара.", "case_document_id": "d1",
           "why_ru": "Технология на стадии исследований, интерес растёт, есть инвестиции в пилоты.",
           "companies": ["Lumotive", "Выдуманная Корпорация"], "stage": 2, "trend": 3,
           "stage_reason_ru": "Лабораторная демонстрация.", "trend_reason_ru": "Инвестиции 45 млн.",
           "source_summaries": [{"document_id": "d1", "summary_ru": "Показан фотонный лидар."},
                                {"document_id": "d9", "summary_ru": "Лишнее."}]}
    row.update(overrides)
    return row


class FinalizeCardsTest(unittest.IsolatedAsyncioTestCase):
    CARD = CardInput("cand-1", "FMCW LiDAR", ("lidar",), SOURCES, 2, 3, "Ранний пилот.")

    def test_check_card_grounds_companies_case_and_summaries(self) -> None:
        card, reason = check_card(self.CARD, card_row("k1"))
        self.assertEqual(reason, "")
        self.assertEqual(card.companies, ("Lumotive",))
        self.assertEqual(card.case_document_id, "doc-1")
        self.assertEqual(card.source_summaries, (("doc-1", "Показан фотонный лидар.", "GENERATIVE_SUMMARY"),))

    def test_check_card_rejects_unsupported_numbers_and_english(self) -> None:
        self.assertIsNone(check_card(self.CARD, card_row("k1", advantage_ru="Экономия 37 процентов энергии."))[0])
        self.assertIsNone(check_card(self.CARD, card_row("k1", why_ru="Early stage photonic lidar with investments."))[0])

    async def test_batch_accepts_valid_and_reports_rejected(self) -> None:
        answer = json.dumps({"cards": [card_row("k1"), card_row("k2", description_ru="Точность 99 процентов."),
                                       card_row("k7")]}, ensure_ascii=False)
        provider = FakeProvider(responses=[answer])
        use_case = FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS))
        cards = [self.CARD, CardInput("cand-2", "X", (), SOURCES), CardInput("cand-3", "Y", (), SOURCES)]
        outcome = await use_case.execute("лидары", cards)
        self.assertEqual([card.candidate_id for card in outcome.cards], ["cand-1"])
        self.assertIn("числа", outcome.rejected["cand-2"])
        self.assertEqual(outcome.rejected["cand-3"], "нет в ответе модели")
        self.assertIn("составь на русском языке", provider.calls[0][0].content)
        self.assertEqual(json.loads(provider.calls[0][1].content)["cards"][0]["sources"][0]["id"], "d1")
        self.assertEqual(FINALIZE_PROMPT_VERSION, "finalize_v1")

    async def test_failed_batch_rejects_all(self) -> None:
        provider = FakeProvider(responses=["{"])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertTrue(outcome.used_fallback)
        self.assertIn("пачка отклонена", outcome.rejected["cand-1"])


if __name__ == "__main__":
    unittest.main()
