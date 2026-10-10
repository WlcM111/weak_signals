"""Этап 4: технологии из расширения передаются в сбор."""

from __future__ import annotations

import unittest

from orchestrator.application.dto import ExpansionView
from orchestrator.application.stages.expand import Glossary, run_expand


class _Insight:
    async def expand_query(self, query_text: str) -> ExpansionView:
        return ExpansionView(ru_terms=("а",), en_terms=("b",), technologies=("x402", " x402 ", "KV cache offloading", "z"))


class ExpandTechnologiesTest(unittest.IsolatedAsyncioTestCase):
    async def test_technologies_cleaned_and_passed(self) -> None:
        view = await run_expand(_Insight(), "финтех", Glossary({}))
        self.assertEqual(view.technologies, ("x402", "KV cache offloading"))

    async def test_fallback_has_no_technologies(self) -> None:
        view = await run_expand(None, "финтех", Glossary({}))
        self.assertEqual(view.technologies, ())


if __name__ == "__main__":
    unittest.main()
