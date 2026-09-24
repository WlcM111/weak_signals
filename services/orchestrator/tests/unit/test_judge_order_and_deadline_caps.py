"""Порядок выдачи при смысловой оценке и отмена анализа по таймауту стадии."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from orchestrator.application.stages.analyze import AnalyzeConfig, run_analyze
from orchestrator.application.stages.expand import Glossary
from orchestrator.application.stages.narrate import apply_judgement
from orchestrator.application.use_cases.run_job import _bounded_expand
from orchestrator.domain.errors import StageFailed


def candidate(cid: str, rank: int) -> SimpleNamespace:
    return SimpleNamespace(candidate_id=cid, rank=rank)


def verdict(kind: str, relevance: int) -> SimpleNamespace:
    return SimpleNamespace(verdict=kind, relevance=relevance, reason_ru="")


class JudgeOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.weak = [candidate("a", 1), candidate("b", 2), candidate("c", 3), candidate("d", 4)]
        self.verdicts = {"a": verdict("EMERGING_TECHNOLOGY", 1), "b": verdict("OFF_TOPIC", 3),
                         "c": verdict("EMERGING_TECHNOLOGY", 3), "d": verdict("EMERGING_TECHNOLOGY", 2)}

    def test_ml_order_keeps_model_ranking_and_llm_only_excludes(self) -> None:
        kept, rejected = apply_judgement(self.weak, self.verdicts, "ml")
        self.assertEqual([c.candidate_id for c in kept], ["a", "c", "d"])
        self.assertEqual([c.candidate_id for c, _ in rejected], ["b"])

    def test_llm_order_is_previous_behaviour(self) -> None:
        kept, _ = apply_judgement(self.weak, self.verdicts, "llm")
        self.assertEqual([c.candidate_id for c in kept], ["c", "d", "a"])


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _NeverFinishing:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def start_analysis(self, **_: object) -> str:
        return "an-1"

    async def get_analysis(self, analysis_id: str) -> SimpleNamespace:
        return SimpleNamespace(is_terminal=False, status="RUNNING", weak_signals_total=0, error_code="")

    async def cancel_analysis(self, analysis_id: str, reason: str) -> None:
        self.cancelled.append(analysis_id)


class _SlowInsight:
    async def expand_query(self, query_text: str) -> None:
        await asyncio.sleep(5)


class DeadlineCapsTest(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_timeout_cancels_upstream_analysis(self) -> None:
        analyzer = _NeverFinishing()

        async def check() -> None:
            return None

        with self.assertRaises(StageFailed):
            await run_analyze(analyzer, "job-1", "col-1", "q", 10, AnalyzeConfig(timeout_seconds=30, poll_seconds=3.0),
                              _Clock(), check)
        self.assertEqual(analyzer.cancelled, ["an-1"])

    async def test_expand_timeout_falls_back_to_glossary_terms(self) -> None:
        expansion = await _bounded_expand(_SlowInsight(), "квантовые сенсоры", Glossary({}), 0.05)
        self.assertTrue(expansion.used_fallback)
