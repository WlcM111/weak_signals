"""Рубричная оценка и пакетная доводка карточек: сопоставление номеров, проверки по источникам, отказоустойчивость."""

from __future__ import annotations

import json
import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.prompt_builder import FINALIZE_PROMPT_VERSION, RUBRIC_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.finalize_cards import CardInput, FinalizeCards, check_card, title_terms
from insight.application.use_cases.rubric_judge import RubricItem, RubricJudge, RubricSource, to_verdict

from .test_chain_and_use_cases import PROMPTS, SCHEMAS, build_chain

SOURCES = (
    RubricSource("doc-1", "Chip-scale FMCW LiDAR engine reaches 200 m range", "openalex", "SCIENTIFIC_PUBLICATION",
                 "HIGH", "2025-03-01", "en", "Researchers at EPFL demonstrated a photonic LiDAR engine with 200 m range.",
                 "nature.com"),
    RubricSource("doc-2", "Стартап Lumotive привлёк инвестиции", "rss", "INDUSTRY_MEDIA", "MEDIUM", "2026-01-10", "ru",
                 "Компания Lumotive привлекла 45 млн долларов на пилоты FMCW-лидаров.", "rb.ru"),
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

    async def test_final_check_prompt_has_strict_ai_security_scope(self) -> None:
        answer = json.dumps({"verdicts": [verdict_row("c1")]}, ensure_ascii=False)
        judge, provider = self.judge(answer, answer)
        await judge.execute("защита систем ИИ", ITEMS[:1], final_check=True)
        await judge.execute("защита систем ИИ", ITEMS[:1])
        final, first = provider.calls[0][0].content, provider.calls[1][0].content
        self.assertIn("Финальная проверка готовой карточки", final)
        for scope in ("психотерапия", "созданных ИИ", "промпт-инъекций", "выбери N-OFF"):
            self.assertIn(scope, final)
        self.assertNotIn("Финальная проверка", first)  # первая рубрика — прежний текст judge_v5
        self.assertEqual(RUBRIC_PROMPT_VERSION, "judge_v6")

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

    def test_a1_filter_drops_unrelated_sources_but_never_the_card(self) -> None:
        from insight.application.use_cases.finalize_cards import relevant_summaries

        unrelated = RubricSource("doc-3", "Teaching Reinforcement Learning to High-School Students", "arxiv", "PREPRINT",
                                 "HIGH", "2026-09-01", "en", "Curriculum for schools.", "arxiv.org")
        russian = RubricSource("doc-4", "Лидары на чипе", "habr", "INDUSTRY_MEDIA", "MEDIUM", "2026-09-01", "ru",
                               "Обзор рынка лидаров.", "habr.com")
        sources = (SOURCES[0], unrelated, russian)
        summaries = {s.document_id: (s.document_id, "Резюме источника на русском.", "GENERATIVE_SUMMARY") for s in sources}
        kept = relevant_summaries(sources, summaries, "Фотонный лидар на чипе (FMCW LiDAR)", "")
        self.assertEqual([k[0] for k in kept], ["doc-1", "doc-4"])  # школьная программа отброшена, русский текст остался
        self.assertEqual([k[0] for k in relevant_summaries(sources, summaries, "Лидар (FMCW LiDAR)", "doc-3")],
                         ["doc-1", "doc-3", "doc-4"])  # источник кейса остаётся всегда
        self.assertEqual(len(relevant_summaries(sources, summaries, "Модель (LLM)", "")), 3)  # нет значимых слов — все
        self.assertEqual(len(relevant_summaries((unrelated,), {"doc-3": summaries["doc-3"]}, "Лидар (FMCW LiDAR)", "")), 1)

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
        self.assertEqual(FINALIZE_PROMPT_VERSION, "finalize_v5")

    async def test_failed_batch_rejects_all(self) -> None:
        # Этап 3 (B5): ошибка JSON повторяется один раз; повторная ошибка оставляет карточку отклонённой.
        provider = FakeProvider(responses=["{", "{"])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertTrue(outcome.used_fallback)
        self.assertIn("пачка отклонена", outcome.rejected["cand-1"])
        self.assertEqual(len(provider.calls), 2)

    async def test_rejected_card_is_retried_once_with_the_reason(self) -> None:
        bad = json.dumps({"cards": [card_row("k1", title_ru="Фотонный лидар на чипе")]}, ensure_ascii=False)
        good = json.dumps({"cards": [card_row("k1")]}, ensure_ascii=False)
        provider = FakeProvider(responses=[bad, good])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertEqual([card.candidate_id for card in outcome.cards], ["cand-1"])
        self.assertEqual(outcome.rejected, {})
        retry = json.loads(provider.calls[1][1].content)["cards"][0]
        self.assertIn("оригинального термина", retry["исправление"])
        self.assertNotIn("исправление", json.loads(provider.calls[0][1].content)["cards"][0])

    async def test_second_rejection_is_final(self) -> None:
        bad = json.dumps({"cards": [card_row("k1", title_ru="Фотонный лидар на чипе")]}, ensure_ascii=False)
        provider = FakeProvider(responses=[bad, bad, bad])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertIn("оригинального термина", outcome.rejected["cand-1"])
        self.assertEqual(len(provider.calls), 2)

    async def test_provider_failure_is_not_retried(self) -> None:
        from insight.domain.errors import ProviderTimeout  # noqa: PLC0415
        from insight.application.use_cases.finalize_cards import repairable  # noqa: PLC0415

        self.assertFalse(repairable("пачка отклонена: ProviderError"))
        self.assertFalse(repairable("модель недоступна"))
        self.assertTrue(repairable("пачка отклонена: OutputRejected"))
        provider = FakeProvider(errors=[ProviderTimeout("медленно", "fake")])
        outcome = await FinalizeCards(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)).execute("q", [self.CARD])
        self.assertIn("пачка отклонена: Provider", outcome.rejected["cand-1"])
        self.assertEqual(len(provider.calls), 1)



class TitleTermsTest(unittest.TestCase):
    """Правило термина, шаг 2: допустимые термины из заголовков источников карточки."""

    TITLES = (
        "ForeTac-VLA: Tactile Foresight for Vision-Language-Action Robot Policies",
        "Optical Circuit Switching (OCS) for AI clusters",
        "A Review on Quantum Approximate Optimization Algorithm and its Variants",
        "SiN-on-SOI Optical Phased Array LiDAR for Ultra-Wide Field of View and 4D Sensing",
        "Possible consequences of the development of smart production systems “Smart Factory”",
        "Цифровой беспроводной неинвазивный глюкометр",
        "Применение архитектур YOLO для распознавания объектов в задачах промышленного мониторинга",
        "Don't Train From Scratch: Persistent Gains from Pruning-Initialised Distillation for Edge Language Models",
        "edward-playground/aidefend-mcp",
    )

    def test_terms_are_verbatim_and_pass_the_term_check(self) -> None:
        from insight.application.use_cases.finalize_cards import TERM_RE, normalize_name, term_in_sources

        for title in self.TITLES:
            terms = title_terms(title)
            with self.subTest(title=title):
                self.assertTrue(terms)
                self.assertLessEqual(len(terms), 6)
                for term in terms:
                    self.assertTrue(term_in_sources(term, normalize_name(title), title), term)
                    self.assertEqual(TERM_RE.findall(f"Название технологии ({term})"), [term])

    def test_names_acronyms_and_noun_groups_come_first(self) -> None:
        self.assertEqual(title_terms(self.TITLES[0])[0], "ForeTac-VLA")
        self.assertEqual(title_terms(self.TITLES[1])[:2], ("Optical Circuit Switching", "OCS"))
        self.assertIn("Quantum Approximate Optimization Algorithm", title_terms(self.TITLES[2]))
        self.assertNotIn("Review", title_terms(self.TITLES[2]))  # одно общее слово — не термин
        self.assertIn("SiN-on-SOI Optical Phased Array LiDAR", title_terms(self.TITLES[3]))
        self.assertIn("Smart Factory", title_terms(self.TITLES[4]))
        self.assertEqual(title_terms(self.TITLES[5]), ("Цифровой беспроводной неинвазивный глюкометр",))
        self.assertIn("YOLO", title_terms(self.TITLES[6]))
        self.assertIn("Pruning-Initialised Distillation", title_terms(self.TITLES[7]))
        self.assertEqual(title_terms(self.TITLES[8]), ("aidefend-mcp",))
        self.assertEqual(title_terms(""), ())

    def test_payload_lists_terms_and_card_with_listed_term_passes(self) -> None:
        from insight.application.use_cases.finalize_cards import card_payload

        payload = card_payload("k1", FinalizeCardsTest.CARD)
        self.assertEqual([s["terms"] for s in payload["sources"]], [list(title_terms(s.title)) for s in SOURCES])
        term = payload["sources"][0]["terms"][0]
        card, reason = check_card(FinalizeCardsTest.CARD, card_row("k1", title_ru=f"Лидарный модуль на чипе ({term})"))
        self.assertEqual(reason, "")
        self.assertIn(term, card.title_ru)


class TermInSourcesTest(unittest.TestCase):
    def test_term_matches_by_meaning_not_only_verbatim(self) -> None:
        from insight.application.use_cases.finalize_cards import normalize_name, term_in_sources

        src = "Kolmogorov-Arnold Networks-Based Tolerance-Aware Manufacturability Assessment"
        for term in ("Tolerance-Aware Manufacturability Assessment", "Tolerance Aware Manufacturability",
                     "Manufacturability Assessment with Tolerances"):
            self.assertTrue(term_in_sources(term, normalize_name(src), src), term)
        for term in ("Quantum Radar", "LLM Firewall", "для"):
            self.assertFalse(term_in_sources(term, normalize_name(src), src), term)

if __name__ == "__main__":
    unittest.main()
