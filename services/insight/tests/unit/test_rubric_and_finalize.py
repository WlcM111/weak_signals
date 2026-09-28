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
    def judge(self, *responses: str, batch: int = 4) -> tuple[RubricJudge, FakeProvider]:
        provider = FakeProvider(responses=list(responses))
        return RubricJudge(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS), batch_size=batch), provider

    async def test_batches_map_short_ids_and_keep_criteria(self) -> None:
        answers = [json.dumps({"verdicts": [verdict_row(f"c{n}") for n in range(1, 5)]}, ensure_ascii=False)] * 2
        answers.append(json.dumps({"verdicts": [verdict_row("c1", "N-OVR", ok=False), verdict_row("c2")]},
                                  ensure_ascii=False))
        judge, provider = self.judge(*answers)
        outcome = await judge.execute("твердотельные лидары", ITEMS)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual([v.candidate_id for v in outcome.verdicts], [f"cand-{n}" for n in range(1, 11)])
        self.assertEqual(outcome.verdicts[8].code, "N-OVR")
        self.assertIn('Слабый сигнал — code "R"', provider.calls[0][0].content)
        payload = json.loads(provider.calls[0][1].content)
        self.assertEqual(payload["candidates"][0]["sources"][1]["site"], "rb.ru")
        self.assertNotIn("doc-1", provider.calls[0][1].content)

    async def test_types_are_coerced_and_bad_verdict_drops_alone(self) -> None:
        loose = verdict_row("c1")
        loose.update(stage="3", trend=5, confidence="0.9", on_topic="true", extra="лишнее", reason_ru="Причина. " * 60)
        broken = verdict_row("c2")
        broken["code"] = "НЕВЕРНО"
        judge, _ = self.judge(json.dumps({"verdicts": [loose, broken]}, ensure_ascii=False),
                              json.dumps({"verdicts": [verdict_row("c1", "N-GEN", ok=False)]}, ensure_ascii=False))
        outcome = await judge.execute("q", ITEMS[:2])
        first = outcome.verdicts[0]
        self.assertEqual((first.candidate_id, first.stage, first.trend, first.confidence), ("cand-1", 3, 3, 0.9))
        self.assertLessEqual(len(first.reason_ru), 300)
        self.assertEqual((outcome.verdicts[1].candidate_id, outcome.verdicts[1].code), ("cand-2", "N-GEN"))

    async def test_failed_batch_is_retried_one_by_one(self) -> None:
        single = json.dumps({"verdicts": [verdict_row("c1")]}, ensure_ascii=False)
        judge, provider = self.judge("не json", single, single)
        outcome = await judge.execute("q", ITEMS[:2])
        self.assertEqual([v.candidate_id for v in outcome.verdicts], ["cand-1", "cand-2"])
        self.assertEqual(len(provider.calls), 3)

    async def test_all_failed_is_fallback(self) -> None:
        judge, _ = self.judge("не json", "не json", "не json")
        self.assertTrue((await judge.execute("q", ITEMS[:2])).used_fallback)

    def test_technology_and_profile_are_extracted_and_optional(self) -> None:
        from insight.application.use_cases.rubric_judge import coerce_verdict

        row = verdict_row("c1")
        row.update(technology_ru="Фотонный FMCW-лидар", profile_ru="Первые пилоты в 2026 году. " * 80)
        clean = coerce_verdict(row)
        verdict = to_verdict("x", clean)
        self.assertEqual(verdict.technology_ru, "Фотонный FMCW-лидар")
        self.assertLessEqual(len(verdict.profile_ru), 700)
        bare = coerce_verdict(verdict_row("c1"))
        self.assertEqual((bare["technology_ru"], bare["profile_ru"]), ("", ""))

    def test_r_without_all_criteria_becomes_u(self) -> None:
        row = verdict_row("c1")
        row["verifiable"] = False
        self.assertEqual(to_verdict("x", row).code, "U")
        self.assertEqual(to_verdict("x", verdict_row("c1")).verdict, "EMERGING_TECHNOLOGY")


def card_row(short: str, **overrides) -> dict:
    row = {"candidate_id": short, "source_ids": ["d1", "d2"], "title_ru": "Фотонный лидар на чипе (FMCW LiDAR)",
           "description_ru": "Интегральный фотонный лидар с частотной модуляцией измеряет дальность до 200 м.",
           "advantage_ru": "Компактность и отсутствие движущихся частей.",
           "case_example_ru": "Исследователи EPFL показали фотонный движок лидара.", "case_document_id": "d1",
           "why_ru": "Технология на стадии исследований, интерес растёт, есть инвестиции в пилоты.",
           "companies": ["«Lumotive»", "Выдуманная Корпорация"],
           "stage_reason_ru": "Лабораторная демонстрация.", "trend_reason_ru": "Инвестиции 45 млн долларов.",
           "source_summaries": [{"document_id": "d1", "summary_ru": "Показан фотонный лидар для 6G-сетей."},
                                {"document_id": "d2", "summary_ru": "Lumotive привлекла инвестиции в пилоты."},
                                {"document_id": "d9", "summary_ru": "Лишнее."}]}
    row.update(overrides)
    return row


class FinalizeCardsTest(unittest.IsolatedAsyncioTestCase):
    CARD = CardInput("cand-1", "FMCW LiDAR", ("lidar",), SOURCES, 2, 3, "Ранний пилот.")

    def test_check_card_grounds_companies_case_and_summaries(self) -> None:
        card, reason = check_card(self.CARD, card_row("k1"))
        self.assertEqual(reason, "")
        self.assertEqual(card.companies, ("«Lumotive»",))
        self.assertEqual(card.case_document_id, "doc-1")
        self.assertEqual([s[0] for s in card.source_summaries], ["doc-1", "doc-2"])
        self.assertEqual([s[2] for s in card.source_summaries], ["GENERATIVE_SUMMARY", "ORIGINAL_RU"])

    def test_only_chosen_sources_and_original_term_required(self) -> None:
        card, _ = check_card(self.CARD, card_row("k1", source_ids=["d1"], trend_reason_ru="Публикации появляются регулярно.",
                                                 source_summaries=[{"document_id": "d1",
                                                                    "summary_ru": "Показан фотонный лидар для 6G-сетей."}]))
        self.assertEqual([s[0] for s in card.source_summaries], ["doc-1"])  # второй источник отброшен
        self.assertIn("не выбраны", check_card(self.CARD, card_row("k1", source_ids=[]))[1])
        self.assertIn("оригинального термина", check_card(self.CARD, card_row("k1", title_ru="Фотонный лидар на чипе"))[1])
        self.assertIn("оригинального термина", check_card(self.CARD, card_row("k1", title_ru="Лидар (Quantum Radar)"))[1])

    def test_unsupported_number_removes_sentence_not_card(self) -> None:
        card, _ = check_card(self.CARD, card_row("k1", advantage_ru="Компактность без движущихся частей. "
                                                                    "Экономия 37 процентов энергии."))
        self.assertEqual(card.advantage_ru, "Компактность без движущихся частей.")

    def test_rejects_english_or_empty_fields_and_missing_summaries(self) -> None:
        self.assertIn("не на русском", check_card(self.CARD, card_row("k1", why_ru="Early stage photonic lidar."))[1])
        self.assertIn("пустое", check_card(self.CARD, card_row("k1", advantage_ru="Экономия 37 процентов."))[1])
        row = card_row("k1")
        row["source_summaries"] = row["source_summaries"][:1]
        self.assertIn("нет резюме", check_card(self.CARD, row)[1])

    async def test_batch_accepts_valid_and_retries_rejected_one_by_one(self) -> None:
        first = json.dumps({"cards": [card_row("k1"), card_row("k2", why_ru="English only text here.")]},
                           ensure_ascii=False)
        retry = json.dumps({"cards": [card_row("k1")]}, ensure_ascii=False)
        provider = FakeProvider(responses=[first, retry])
        use_case = FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS))
        outcome = await use_case.execute("лидары", [self.CARD, CardInput("cand-2", "X", (), SOURCES)])
        self.assertEqual(sorted(card.candidate_id for card in outcome.cards), ["cand-1", "cand-2"])
        self.assertEqual(outcome.rejected, {})
        self.assertIn("составь", provider.calls[0][0].content.replace("заполни", "составь"))
        self.assertEqual(FINALIZE_PROMPT_VERSION, "finalize_v4")

    async def test_failed_batch_rejects_all(self) -> None:
        provider = FakeProvider(responses=["{"])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertTrue(outcome.used_fallback)
        self.assertIn("пачка отклонена", outcome.rejected["cand-1"])


if __name__ == "__main__":
    unittest.main()
