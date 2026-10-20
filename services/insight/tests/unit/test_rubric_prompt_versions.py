"""Этап 5.2 (R-013): версия промпта рубрики выбирается настройкой; judge_v7 добавляет критерий зрелости."""

from __future__ import annotations

import unittest
from pathlib import Path

from insight.application.prompt_builder import RUBRIC_FINAL_PROMPT_VERSION, RUBRIC_PROMPT_VERSION, PromptBuilder

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
        with self.assertRaises(ValueError):
            PromptBuilder(PROMPTS, SCHEMAS, "judge_v6", "judge_v99")

    def test_final_version_defaults_to_rubric_version(self) -> None:
        builder = PromptBuilder(PROMPTS, SCHEMAS, "judge_v8")
        bundle = builder.build_rubric_prompt("запрос", CANDIDATE, final_check=True)
        self.assertEqual((builder.final_version, bundle.prompt_version), ("judge_v8", "judge_v8"))

    def test_final_version_only_for_final_check(self) -> None:
        # этап 5.4: отбор пула — прежней версией, финальная проверка готовой карточки — judge_v11
        builder = PromptBuilder(PROMPTS, SCHEMAS, "judge_v6", RUBRIC_FINAL_PROMPT_VERSION)
        pool = builder.build_rubric_prompt("запрос", CANDIDATE)
        final = builder.build_rubric_prompt("запрос", CANDIDATE, final_check=True)
        self.assertEqual((pool.prompt_version, final.prompt_version), ("judge_v6", "judge_v11"))
        self.assertEqual(pool.messages[0].content,
                         PromptBuilder(PROMPTS, SCHEMAS).build_rubric_prompt("запрос", CANDIDATE).messages[0].content)
        text = final.messages[0].content
        self.assertIn("Финальная проверка готовой карточки", text)
        self.assertIn("уже массово используется на практике", text)
        # judge_v9 терял слабые сигналы-исследования: многолетняя активность исследований — не зрелость
        self.assertIn("без массового практического\n   применения зрелостью не считается", text)
        self.assertIn("N-MAT выбирай, только если уверен в массовом практическом применении", text)
        self.assertIn("не делает технологию зрелой", text)  # раунд и запуск продукта — не зрелость (как в judge_v8)
        self.assertNotIn("больше 5–7 лет", text)
        self.assertTrue({"judge_v9", "judge_v10", "judge_v11"} <= {d.prompt_version for d in builder.definitions()})
        # judge_v10 требовал N-GEN для карточки-области и на наборе организаторов задевал широкие сигналы
        self.assertNotIn("это N-GEN, а если такая область", text)
        self.assertIn("если\nкарточка называет область целиком и эта область массово применяется на практике", text)

    def test_v9_keeps_research_age_rule_for_comparison(self) -> None:
        text = PromptBuilder(PROMPTS, SCHEMAS, "judge_v6", "judge_v9").build_rubric_prompt(
            "запрос", CANDIDATE, final_check=True).messages[0].content
        self.assertIn("больше 5–7 лет", text)

if __name__ == "__main__":
    unittest.main()
