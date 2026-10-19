"""Этап 5.4: второй проход зрелости — разбор ответа, сверка по эху названия, правило, встраивание в рубрику.

Ключевые свойства: сбой модели или ответ не про того кандидата не меняют вердикт; кандидаты с сообщениями СМИ не
проверяются и не отсеиваются; отсеивается только принятый (R) кандидат с научными источниками по заданному правилу.
"""

from __future__ import annotations

import json
import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.prompt_builder import PromptBuilder
from insight.application.use_cases.maturity_check import (
    MaturityAssessment,
    MaturityCheck,
    MaturityInput,
    MaturityRule,
    echo_matches,
    research_only,
)
from insight.application.use_cases.rubric_judge import RubricItem, RubricJudge, RubricSource

from .test_chain_and_use_cases import PROMPTS, SCHEMAS, build_chain

PAPER = RubricSource("d1", "Uncalibrated visual servoing with recursive least squares", "openalex",
                     "SCIENTIFIC_PUBLICATION", "HIGH", "2026-03-01", "en", "We improve a known controller.", "doi.org")
PREPRINT = RubricSource("d2", "Speculative decoding for edge LLM serving", "arxiv", "PREPRINT", "HIGH", "2026-04-01",
                        "en", "A new drafting mechanism for on-device decoding.", "arxiv.org")
NEWS = RubricSource("d3", "Startup raises $20M for robot grippers", "rss", "NEWS", "MEDIUM", "2026-05-01", "en",
                    "The company will scale production.", "techcrunch.com")
ITEMS = [RubricItem("cand-1", "Визуальное сервоуправление кабельными роботами", (), (PAPER,)),
         RubricItem("cand-2", "Спекулятивное декодирование на периферии", (), (PREPRINT,)),
         RubricItem("cand-3", "Захваты для роботов стартапа", (), (NEWS,))]


def rubric_answer() -> str:
    rows = [{"candidate_id": f"c{n}", "code": "R", "on_topic": True, "concrete": True, "early_stage": True,
             "verifiable": True, "stage": 2, "trend": 2, "relevance": 3, "confidence": 0.8, "reason_ru": "Причина.",
             "technology_ru": title, "profile_ru": "Описание."}
            for n, title in enumerate(("Визуальное сервоуправление кабельными роботами",
                                       "Спекулятивное декодирование на периферии", "Захваты для роботов стартапа"), 1)]
    return json.dumps({"verdicts": rows}, ensure_ascii=False)


def maturity_answer(rows: list[tuple[str, str, int, bool, str]]) -> str:
    return json.dumps({"items": [{"id": sid, "title_echo": echo, "task_en": "visual servoing", "task_since_year": year,
                                  "task_established": established, "novelty_type": novelty}
                                 for sid, echo, year, established, novelty in rows]}, ensure_ascii=False)


class MaturityCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_misaligned_answer_is_dropped(self) -> None:
        provider = FakeProvider(responses=[maturity_answer([
            ("m1", "Визуальное сервоуправление кабельными", 1990, True, "improvement"),
            ("m2", "Совсем другое название", 2023, False, "new_mechanism")])])
        check = MaturityCheck(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS))
        found = await check.assess([MaturityInput("a", "Визуальное сервоуправление кабельными роботами"),
                                    MaturityInput("b", "Спекулятивное декодирование на периферии")])
        self.assertEqual(set(found), {"a"})
        self.assertEqual(check.stats["misaligned"], 1)

    async def test_unparseable_answer_fails_open(self) -> None:
        check = MaturityCheck(build_chain([FakeProvider(responses=["не JSON"])]), PromptBuilder(PROMPTS, SCHEMAS))
        self.assertEqual(await check.assess([MaturityInput("a", "Название")]), {})
        self.assertEqual(check.stats["failed_batches"], 1)

    def test_rule_and_scope(self) -> None:
        rule = MaturityRule(2015, frozenset({"improvement", "survey"}))
        self.assertTrue(rule.mature(MaturityAssessment("a", "t", 1990, True, "improvement")))
        self.assertFalse(rule.mature(MaturityAssessment("a", "t", 1990, True, "new_mechanism")))  # новое внутри зрелого
        self.assertFalse(rule.mature(MaturityAssessment("a", "t", 2021, True, "improvement")))   # молодая задача
        self.assertFalse(rule.mature(MaturityAssessment("a", "t", 0, True, "improvement")))      # год неизвестен
        self.assertFalse(rule.mature(MaturityAssessment("a", "t", 1990, False, "improvement")))  # узкая задача
        self.assertTrue(research_only(["SCIENTIFIC_PUBLICATION", "PREPRINT"]))
        self.assertFalse(research_only(["PREPRINT", "NEWS"]))
        self.assertFalse(research_only([]))
        self.assertTrue(echo_matches("Визуальное сервоуправление", "Визуальное сервоуправление кабельными роботами"))
        self.assertEqual(MaturityRule.parse_novelty("improvement, bogus"), frozenset({"improvement"}))


class RubricSecondPassTest(unittest.IsolatedAsyncioTestCase):
    def judge(self, *answers: str, enabled: bool = True) -> tuple[RubricJudge, FakeProvider]:
        provider = FakeProvider(responses=list(answers))
        chain, prompts = build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)
        maturity = (MaturityCheck(chain, prompts), MaturityRule(2015)) if enabled else None
        return RubricJudge(chain, prompts, batch_size=4, maturity=maturity), provider

    async def test_only_research_candidates_are_checked_and_only_mature_rule_excludes(self) -> None:
        judge, provider = self.judge(rubric_answer(), maturity_answer([
            ("m1", "Визуальное сервоуправление кабельными роботами", 1990, True, "improvement"),
            ("m2", "Спекулятивное декодирование на периферии", 2023, False, "new_mechanism")]))
        outcome = await judge.execute("робототехника", ITEMS)
        codes = {v.candidate_id: v.code for v in outcome.verdicts}
        self.assertEqual(codes, {"cand-1": "N-MAT", "cand-2": "R", "cand-3": "R"})
        self.assertTrue(outcome.verdicts[0].reason_ru.startswith("Проверка зрелости:"))
        sent = json.loads(provider.calls[1][1].content)["candidates"]
        self.assertEqual(len(sent), 2)  # кандидат с новостью в проверку не отправляется

    async def test_failure_of_second_pass_keeps_rubric_verdicts(self) -> None:
        judge, _ = self.judge(rubric_answer(), "не JSON")
        outcome = await judge.execute("робототехника", ITEMS)
        self.assertEqual([v.code for v in outcome.verdicts], ["R", "R", "R"])

    async def test_disabled_makes_no_extra_call(self) -> None:
        judge, provider = self.judge(rubric_answer(), enabled=False)
        outcome = await judge.execute("робототехника", ITEMS)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual([v.code for v in outcome.verdicts], ["R", "R", "R"])

    async def test_final_check_is_not_affected(self) -> None:
        judge, provider = self.judge(rubric_answer())
        await judge.execute("робототехника", ITEMS, final_check=True)
        self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
