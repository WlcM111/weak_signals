"""Смысловая оценка кандидатов: сопоставление коротких номеров, отказоустойчивость, регистрация промпта."""

from __future__ import annotations

import json
import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.prompt_builder import JUDGE_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.judge_candidates import JudgeCandidates, JudgeItem
from insight.domain.values import Purpose

from .test_chain_and_use_cases import PROMPTS, SCHEMAS, build_chain

ITEMS = [
    JudgeItem(
        "11111111-aaaa", "Квантовые сенсоры на NV-центрах", ("nv center", "magnetometer"), ("Diamond NV magnetometry.",)
    ),
    JudgeItem("22222222-bbbb", "Развитие технологий ИИ в России", ("ии", "россия"), ("Обзор отрасли.",)),
    JudgeItem("33333333-cccc", "Микропластик в России", ("микропластик",), ("Загрязнение водоёмов.",)),
]


def answer(*rows: tuple[str, str, int]) -> str:
    return json.dumps({"verdicts": [{"candidate_id": c, "verdict": v, "relevance": r, "reason_ru": "Причина."}
                                    for c, v, r in rows]}, ensure_ascii=False)


class JudgeCandidatesTest(unittest.IsolatedAsyncioTestCase):
    def use_case(self, *responses: str) -> tuple[JudgeCandidates, FakeProvider]:
        provider = FakeProvider(responses=list(responses))
        return JudgeCandidates(build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS)), provider

    async def test_short_ids_mapped_back_and_unknown_ignored(self) -> None:
        use_case, _ = self.use_case(answer(
            ("c1", "EMERGING_TECHNOLOGY", 3), ("c2", "OVERVIEW", 1), ("c3", "OFF_TOPIC", 0),
            ("c9", "NOISE", 0), ("c1", "NOISE", 0)))
        outcome = await use_case.execute("квантовые сенсоры", ITEMS)
        self.assertFalse(outcome.used_fallback)
        self.assertEqual([(v.candidate_id, v.verdict, v.relevance) for v in outcome.verdicts], [
            ("11111111-aaaa", "EMERGING_TECHNOLOGY", 3),
            ("22222222-bbbb", "OVERVIEW", 1),
            ("33333333-cccc", "OFF_TOPIC", 0),
        ])

    async def test_prompt_uses_short_ids_only(self) -> None:
        use_case, provider = self.use_case(answer(("c1", "EMERGING_TECHNOLOGY", 3)))
        await use_case.execute("квантовые сенсоры", ITEMS)
        user = provider.calls[0][-1].content if hasattr(provider.calls[0][-1], "content") else str(provider.calls[0])
        self.assertIn('"id": "c1"', user)
        self.assertNotIn("11111111-aaaa", user)

    async def test_invalid_answer_falls_back(self) -> None:
        use_case, _ = self.use_case("не JSON")
        outcome = await use_case.execute("квантовые сенсоры", ITEMS)
        self.assertTrue(outcome.used_fallback)
        self.assertEqual(outcome.verdicts, ())

    async def test_unknown_verdict_rejected_by_schema(self) -> None:
        use_case, _ = self.use_case(answer(("c1", "GREAT", 3)))
        self.assertTrue((await use_case.execute("квантовые сенсоры", ITEMS)).used_fallback)

    async def test_no_items_no_call(self) -> None:
        use_case, provider = self.use_case(answer(("c1", "EMERGING_TECHNOLOGY", 3)))
        self.assertTrue((await use_case.execute("квантовые сенсоры", [])).used_fallback)
        self.assertEqual(provider.calls, [])

    def test_prompt_registered_with_judge_purpose(self) -> None:
        definitions = {d.prompt_version: d for d in PromptBuilder(PROMPTS, SCHEMAS).definitions()}
        self.assertIs(definitions[JUDGE_PROMPT_VERSION].purpose, Purpose.JUDGE)


if __name__ == "__main__":
    unittest.main()
