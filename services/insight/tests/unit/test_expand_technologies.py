"""Этап 4: расширение запроса возвращает список конкретных технологий (prompt expand_v2)."""

from __future__ import annotations

import unittest
from pathlib import Path

from insight.adapters.inbound.mappers import to_expand_response
from insight.application.prompt_builder import EXPAND_PROMPT_VERSION, PromptBuilder
from insight.application.dto import LLMResult
from insight.application.use_cases.expand_query import (
    MAX_TECHNOLOGIES,
    ExpandQuery,
    clean_technologies,
    technologies_payload,
)
from insight.domain.entities import QueryExpansion

ROOT = Path(__file__).resolve().parents[2]


class ExpandTechnologiesTest(unittest.TestCase):
    def test_clean_drops_generic_long_and_duplicates(self) -> None:
        items = ["Sodium-ion battery cathodes", "sodium-ion  battery cathodes", "Machine Learning", "x",
                 "Solid-state transformers.", "a b c d e f g", "edge AI", "anomaly detection algorithms",
                 "Federated Learning", "AI-powered supply chain resilience", "Intelligent workload orchestration",
                 "Adaptive power management", "federated learning for distributed factories",
                 "Anomaly detection on IoT data streams"]
        # этап 4.6 (аудит P17): новое применение зрелого метода остаётся, вариант с общим уточнением — нет
        self.assertEqual(clean_technologies(items, forbidden=("edge AI",)),
                         ("Sodium-ion battery cathodes", "Solid-state transformers", "Anomaly detection on IoT data streams"))
        self.assertEqual(len(clean_technologies([f"tech {i} cells" for i in range(60)])), MAX_TECHNOLOGIES)

    def test_prompt_v2_asks_for_technologies_and_schema_allows_them(self) -> None:
        bundle = PromptBuilder(ROOT / "prompts", ROOT / "schemas").build_expand_prompt("инфраструктура для ИИ", ("edge",))
        self.assertEqual(EXPAND_PROMPT_VERSION, "expand_v3")
        self.assertEqual(bundle.prompt_version, "expand_v3")
        self.assertEqual(bundle.json_schema["properties"]["technologies"], {})  # список проверяется отдельно
        self.assertNotIn("technologies", bundle.json_schema["required"])  # без списка расширение не уходит в резерв
        self.assertIn('"maxItems": 80', bundle.messages[0].content)  # модель видит полную схему
        system = bundle.messages[0].content
        self.assertIn("КОНКРЕТНЫХ подходов", system)
        self.assertIn("2024–2026", system)
        # честная оценка: в примерах промпта нет технологий экспертного списка (были в expand_v2)
        for leaked in ("optical circuit switching", "KV cache offloading", "tensor network compression", "agent payment"):
            self.assertNotIn(leaked, system)

    def test_response_carries_technologies(self) -> None:
        expansion = QueryExpansion(query_norm="q", ru_terms=("а",), en_terms=("b",), technologies=("x402", "KV cache offloading"))
        self.assertEqual(list(to_expand_response(expansion).technologies), ["x402", "KV cache offloading"])


class TechnologiesPayloadTest(unittest.TestCase):
    """Этап 4.2: ошибка в списке технологий не отбрасывает фразы темы."""

    def setUp(self) -> None:
        self.use_case = ExpandQuery(None, PromptBuilder(ROOT / "prompts", ROOT / "schemas"), None, None)  # type: ignore[arg-type]
        self.result = LLMResult(text="", model="GigaChat-2-Max")

    def expand(self, technologies: object) -> QueryExpansion:
        payload = {"ru_terms": ["промышленный ИИ"], "en_terms": ["industrial AI"], "domain_tags": [],
                   "technologies": technologies}
        return self.use_case._from_payload("промышленный ии", payload, self.result, "gigachat")

    def test_more_than_40_kept_as_first_40(self) -> None:
        expansion = self.expand([f"sensor fusion variant {i}" for i in range(45)])
        self.assertFalse(expansion.used_fallback)
        self.assertEqual(len(expansion.technologies), MAX_TECHNOLOGIES)
        self.assertEqual(expansion.technologies[0], "sensor fusion variant 0")

    def test_more_than_80_or_wrong_type_never_drops_terms(self) -> None:
        many = self.expand([f"sensor fusion variant {i}" for i in range(95)])
        self.assertEqual((many.ru_terms, len(many.technologies)), (("промышленный ИИ",), MAX_TECHNOLOGIES))
        broken = self.expand("not a list")
        self.assertFalse(broken.used_fallback)
        self.assertEqual((broken.en_terms, broken.technologies), (("industrial AI",), ()))

    def test_validation_passes_overlong_list_then_keeps_40(self) -> None:
        import json

        from insight.application.json_output import parse_and_validate

        bundle = PromptBuilder(ROOT / "prompts", ROOT / "schemas").build_expand_prompt("финтех", ())
        body = {"ru_terms": ["финтех"], "en_terms": ["fintech payments"], "domain_tags": [],
                "technologies": [f"deposit token variant {i}" for i in range(95)]}
        payload = parse_and_validate(json.dumps(body, ensure_ascii=False), bundle.json_schema)  # прежде — отказ
        expansion = self.use_case._from_payload("финтех", payload, self.result, "gigachat")
        self.assertEqual((expansion.used_fallback, len(expansion.technologies)), (False, MAX_TECHNOLOGIES))

    def test_payload_report(self) -> None:
        self.assertEqual(technologies_payload(None), ([], 0, None))
        items, returned, problem = technologies_payload(["a b", 3, *[f"t {i}" for i in range(85)]])
        self.assertEqual((len(items), returned), (80, 87))
        self.assertIn("не строк: 1", problem)
        self.assertIn("больше 80", problem)


if __name__ == "__main__":
    unittest.main()
