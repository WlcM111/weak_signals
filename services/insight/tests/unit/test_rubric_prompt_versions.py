"""Этап 5.2 (R-013): версия промпта рубрики выбирается настройкой; judge_v7 добавляет критерий зрелости."""

from __future__ import annotations

import unittest
from pathlib import Path

from insight.application.prompt_builder import RUBRIC_PROMPT_VERSION, PromptBuilder

ROOT = Path(__file__).resolve().parents[2]
PROMPTS, SCHEMAS = ROOT / "prompts", ROOT / "schemas"
CANDIDATE = [{"candidate_id": "c1", "title": "Тест", "keyphrases": [], "sources": [], "composition": ""}]


class RubricVersionTest(unittest.TestCase):
    def test_default_is_unchanged_v6(self) -> None:
        builder = PromptBuilder(PROMPTS, SCHEMAS)
        bundle = builder.build_rubric_prompt("запрос", CANDIDATE)
        self.assertEqual((RUBRIC_PROMPT_VERSION, builder.rubric_version, bundle.prompt_version), ("judge_v6",) * 3)
        self.assertNotIn("Дата публикации стадию не определяет", bundle.messages[0].content)

    def test_v7_adds_maturity_criterion_and_keeps_final_check(self) -> None:
        builder = PromptBuilder(PROMPTS, SCHEMAS, "judge_v7")
        bundle = builder.build_rubric_prompt("запрос", CANDIDATE)
        text = bundle.messages[0].content
        self.assertEqual(bundle.prompt_version, "judge_v7")
        self.assertIn("Дата публикации стадию не определяет", text)
        self.assertIn("новая работа в давно сложившейся задаче", text)
        final = builder.build_rubric_prompt("запрос", CANDIDATE, final_check=True).messages[0].content
        self.assertIn("Финальная проверка готовой карточки", final)

    def test_v8_allows_general_knowledge_only_for_field_age(self) -> None:
        builder = PromptBuilder(PROMPTS, SCHEMAS, "judge_v8")
        text = builder.build_rubric_prompt("запрос", CANDIDATE).messages[0].content
        self.assertEqual(builder.rubric_version, "judge_v8")
        self.assertIn("Исключение — возраст и распространённость области", text)
        self.assertIn("не делает технологию зрелой", text)  # раунд и запуск продукта — не зрелость (ошибка judge_v7)
        self.assertNotIn("Не придумывай фактов и не используй знания вне переданных источников.", text)
        self.assertIn("Финальная проверка готовой карточки",
                      builder.build_rubric_prompt("запрос", CANDIDATE, final_check=True).messages[0].content)

    def test_unknown_version_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PromptBuilder(PROMPTS, SCHEMAS, "judge_v99")


if __name__ == "__main__":
    unittest.main()
