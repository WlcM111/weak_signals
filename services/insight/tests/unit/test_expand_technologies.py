"""Этап 4: расширение запроса возвращает список конкретных технологий (prompt expand_v2)."""

from __future__ import annotations

import unittest
from pathlib import Path

from insight.adapters.inbound.mappers import to_expand_response
from insight.application.prompt_builder import EXPAND_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.expand_query import MAX_TECHNOLOGIES, clean_technologies
from insight.domain.entities import QueryExpansion

ROOT = Path(__file__).resolve().parents[2]


class ExpandTechnologiesTest(unittest.TestCase):
    def test_clean_drops_generic_long_and_duplicates(self) -> None:
        items = ["Sodium-ion battery cathodes", "sodium-ion  battery cathodes", "Machine Learning", "x",
                 "Solid-state transformers.", "a b c d e f g", "edge AI", "anomaly detection algorithms",
                 "Federated Learning", "AI-powered supply chain resilience", "Intelligent workload orchestration",
                 "Adaptive power management"]
        self.assertEqual(clean_technologies(items, forbidden=("edge AI",)),
                         ("Sodium-ion battery cathodes", "Solid-state transformers"))
        self.assertEqual(len(clean_technologies([f"tech {i} cells" for i in range(60)])), MAX_TECHNOLOGIES)

    def test_prompt_v2_asks_for_technologies_and_schema_allows_them(self) -> None:
        bundle = PromptBuilder(ROOT / "prompts", ROOT / "schemas").build_expand_prompt("инфраструктура для ИИ", ("edge",))
        self.assertEqual(EXPAND_PROMPT_VERSION, "expand_v3")
        self.assertEqual(bundle.prompt_version, "expand_v3")
        self.assertIn("technologies", bundle.json_schema["properties"])
        self.assertNotIn("technologies", bundle.json_schema["required"])  # без списка расширение не уходит в резерв
        system = bundle.messages[0].content
        self.assertIn("КОНКРЕТНЫХ подходов", system)
        self.assertIn("2024–2026", system)
        # честная оценка: в примерах промпта нет технологий экспертного списка (были в expand_v2)
        for leaked in ("optical circuit switching", "KV cache offloading", "tensor network compression", "agent payment"):
            self.assertNotIn(leaked, system)

    def test_response_carries_technologies(self) -> None:
        expansion = QueryExpansion(query_norm="q", ru_terms=("а",), en_terms=("b",), technologies=("x402", "KV cache offloading"))
        self.assertEqual(list(to_expand_response(expansion).technologies), ["x402", "KV cache offloading"])


if __name__ == "__main__":
    unittest.main()
