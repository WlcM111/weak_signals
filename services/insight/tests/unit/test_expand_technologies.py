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
        items = ["Optical circuit switching", "optical  circuit switching", "Machine Learning", "x",
                 "KV cache offloading.", "a b c d e f g", "edge AI"]
        self.assertEqual(clean_technologies(items, forbidden=("edge AI",)), ("Optical circuit switching", "KV cache offloading"))
        self.assertEqual(len(clean_technologies([f"tech {i}" for i in range(60)])), MAX_TECHNOLOGIES)

    def test_prompt_v2_asks_for_technologies_and_schema_allows_them(self) -> None:
        bundle = PromptBuilder(ROOT / "prompts", ROOT / "schemas").build_expand_prompt("инфраструктура для ИИ", ("edge",))
        self.assertEqual(EXPAND_PROMPT_VERSION, "expand_v2")
        self.assertEqual(bundle.prompt_version, "expand_v2")
        self.assertIn("technologies", bundle.json_schema["properties"])
        self.assertNotIn("technologies", bundle.json_schema["required"])  # без списка расширение не уходит в резерв
        self.assertIn("КОНКРЕТНЫХ технологий", bundle.messages[0].content)

    def test_response_carries_technologies(self) -> None:
        expansion = QueryExpansion(query_norm="q", ru_terms=("а",), en_terms=("b",), technologies=("x402", "KV cache offloading"))
        self.assertEqual(list(to_expand_response(expansion).technologies), ["x402", "KV cache offloading"])


if __name__ == "__main__":
    unittest.main()
