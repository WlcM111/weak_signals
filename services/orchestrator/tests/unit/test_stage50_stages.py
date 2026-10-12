"""Этап 5.0 (аудит раунда 3): ожидание анализа по объёму (N04), остановка просроченного сбора (N07)."""

from __future__ import annotations

import unittest

from orchestrator.application.stages.analyze import AnalyzeConfig, analysis_timeout
from orchestrator.application.stages.collect import _stop_collection
from orchestrator.domain.errors import StageFailed


class _View:
    def __init__(self, terminal: bool) -> None:
        self.is_terminal = terminal


class _Collector:
    def __init__(self, states: list[bool]) -> None:
        self.states, self.cancelled = list(states), []

    async def cancel_collection(self, collection_id: str, reason: str) -> str:
        self.cancelled.append(collection_id)
        return "CANCELLING"

    async def get_collection(self, collection_id: str) -> _View:
        return _View(self.states.pop(0) if self.states else False)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class AnalysisTimeoutTest(unittest.TestCase):
    def test_scales_with_documents(self) -> None:
        config = AnalyzeConfig(timeout_seconds=600, per_document_seconds=0.5)
        self.assertEqual(analysis_timeout(config, 200), 600)
        self.assertEqual(analysis_timeout(config, 1450), 725)  # прежде всегда 300 с


class StopCollectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_overdue_collection_is_stopped_and_frozen(self) -> None:
        collector = _Collector([False, True])
        view = await _stop_collection(collector, "c-1", _Clock(), 3.0)
        self.assertTrue(view.is_terminal)
        self.assertEqual(collector.cancelled, ["c-1"])

    async def test_collection_that_does_not_stop_fails_the_stage(self) -> None:
        with self.assertRaises(StageFailed):
            await _stop_collection(_Collector([]), "c-2", _Clock(), 3.0)
