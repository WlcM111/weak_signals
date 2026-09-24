"""Регрессионные тесты исправлений аудита: воркер анализов, горячая загрузка модели, проверка Wikipedia."""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
from pathlib import Path

import numpy as np

from analyzer.adapters.inbound.worker import AnalysisWorker, ModelWatcher
from analyzer.application.dto import EncyclopediaHit
from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig, _ClusterView
from analyzer.domain.errors import CollectorUnavailable
from analyzer.domain.values import AnalysisErrorCode, OperationStatus


class _Analyses:
    def __init__(self, fail_claims: int = 0) -> None:
        self.claims = 0
        self.fail_claims = fail_claims
        self.finished: list[tuple[str, OperationStatus, str]] = []

    def claim_next(self, owner: str, lease_seconds: int):  # noqa: ANN201
        self.claims += 1
        if self.claims <= self.fail_claims:
            raise ConnectionError("БД недоступна")
        if self.claims == self.fail_claims + 1:
            return types.SimpleNamespace(analysis_id="a-1")
        return None

    def finish(self, analysis_id, owner, status, *, stats, error_code, error_message, finished_at):  # noqa: ANN001, ANN201
        self.finished.append((analysis_id, status, error_code))
        return True


class _Boom:
    def execute(self, analysis, owner):  # noqa: ANN001, ANN201
        raise RuntimeError("сбой кластеризации")


class WorkerResilienceTest(unittest.TestCase):
    def test_unexpected_error_marks_analysis_failed(self) -> None:
        analyses = _Analyses()
        self.assertTrue(AnalysisWorker(analyses, _Boom(), "w", 60).run_once())
        self.assertEqual(
            analyses.finished, [("a-1", OperationStatus.FAILED, AnalysisErrorCode.INTERNAL_ERROR.value)]
        )

    def test_claim_failure_does_not_kill_worker_loop(self) -> None:
        stop = threading.Event()
        analyses = _Analyses(fail_claims=2)
        original = analyses.claim_next

        def claim_and_stop(owner: str, lease_seconds: int):  # noqa: ANN202
            if analyses.claims >= 3:
                stop.set()  # после двух сбоев БД и одного анализа цикл останавливается сам
            return original(owner, lease_seconds)

        analyses.claim_next = claim_and_stop
        AnalysisWorker(analyses, _Boom(), "w", 60, poll_interval=0).run_forever(stop)
        self.assertEqual(len(analyses.finished), 1)  # цикл пережил сбои БД и дошёл до анализа


class ModelWatcherTest(unittest.TestCase):
    def test_model_is_activated_when_manifest_appears_and_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            state = {"loaded": False, "calls": 0}

            def activate() -> None:
                state["calls"] += 1
                state["loaded"] = True

            watcher = ModelWatcher(activate, lambda: state["loaded"], manifest)
            self.assertFalse(watcher.poll_once())  # манифеста нет — активировать нечего
            manifest.write_text("{\"v\": 1}", encoding="utf-8")
            self.assertTrue(watcher.poll_once())  # модель появилась после старта сервиса
            self.assertFalse(watcher.poll_once())  # тот же манифест повторно не загружается
            manifest.write_text("{\"v\": 2}", encoding="utf-8")
            self.assertTrue(watcher.poll_once())  # переобучение подхвачено без перезапуска
            self.assertEqual(state["calls"], 2)

    def test_broken_manifest_is_retried_limited_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            calls = []

            def activate() -> None:
                calls.append(1)
                raise ValueError("контрольная сумма не совпала")

            watcher = ModelWatcher(activate, lambda: False, manifest)
            for _ in range(10):
                self.assertFalse(watcher.poll_once())
            self.assertEqual(len(calls), 3)  # три попытки на один манифест, затем ожидание нового


class _Collector:
    def __init__(self, fail_batches: int = 0) -> None:
        self.requested: list[str] = []
        self.fail_batches = fail_batches
        self.calls = 0

    def check_encyclopedia(self, titles, language_code):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls <= self.fail_batches:
            raise CollectorUnavailable("collector.CheckEncyclopedia: DEADLINE_EXCEEDED")
        self.requested.extend(titles)
        return [EncyclopediaHit(title=title, exists=True, pageviews_30d=50000) for title in titles]


def _view(index: int, title: str, keyphrases: tuple[str, ...]) -> _ClusterView:
    return _ClusterView(
        cluster_index=index,
        documents=[],
        similarities=[],
        centroid=np.zeros(2, dtype=np.float32),
        title_auto=title,
        keyphrases=keyphrases,
        query_relevance=0.9,
    )


class EncyclopediaSignalsTest(unittest.TestCase):
    def _run(self, collector: _Collector, views: list[_ClusterView]) -> dict:
        stub = types.SimpleNamespace(
            _config=RunAnalysisConfig(encyclopedia_languages=("en",)),
            _collector=collector,
            _log=types.SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        control = types.SimpleNamespace(check=lambda: None)
        return RunAnalysis._encyclopedia_signals(stub, views, control)

    def test_generic_single_words_are_not_probed(self) -> None:
        collector = _Collector()
        views = [_view(0, "prompt injection defense", ("LLM", "security", "agent firewall", "guardrails"))]
        signals = self._run(collector, views)
        self.assertEqual(collector.requested, ["prompt injection defense", "agent firewall"])
        self.assertTrue(signals[0].exists)

    def test_single_word_cluster_gets_no_encyclopedia_signal(self) -> None:
        collector = _Collector()
        self.assertEqual(self._run(collector, [_view(0, "security", ("LLM",))]), {})
        self.assertEqual(collector.calls, 0)

    def test_one_failed_batch_does_not_cancel_the_rest(self) -> None:
        collector = _Collector(fail_batches=1)
        views = [_view(index, f"novel method {index}", ()) for index in range(25)]  # две пачки по 20 и 5
        signals = self._run(collector, views)
        self.assertEqual(len(signals), 5)  # первая пачка отказала, вторая обработана