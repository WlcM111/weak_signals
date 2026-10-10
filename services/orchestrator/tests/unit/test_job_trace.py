"""Трасса задания по стадиям (этап 2, аудит A3)."""

from __future__ import annotations

import dataclasses
import types
import unittest
from datetime import UTC, datetime
from unittest import mock

from orchestrator.application.dto import AnalysisView, CollectionView, ExpansionView
from orchestrator.application.stages.narrate import NarrateOutcome
from orchestrator.application.use_cases import run_job
from orchestrator.application.use_cases.run_job import RunJob, RunJobConfig, plain
from orchestrator.domain.values import JobStatus

from ..fakes import FakeClock, InMemoryResultRepository


def _instance(cls):  # noqa: ANN001, ANN202
    values = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            continue
        values[f.name] = {"str": "x", "int": 1, "bool": True, "float": 0.5}.get(str(f.type).split("|")[0].strip(), "x")
    return cls(**values)


class _Jobs:
    def __init__(self) -> None:
        self.transitions: list = []

    async def transition(self, job, status, worker_id, message=None) -> None:  # noqa: ANN001
        self.transitions.append(status)
        job.status = status

    async def set_references(self, job_id, **kwargs) -> None:  # noqa: ANN001, ANN003
        return None

    async def heartbeat(self, job_id, worker_id, lease_seconds):  # noqa: ANN001, ANN201
        return True, False


class RunJobTraceTest(unittest.IsolatedAsyncioTestCase):
    async def test_job_expand_collect_analyze_are_traced(self) -> None:
        results = InMemoryResultRepository()
        expansion = ExpansionView(ru_terms=("квантовые сенсоры",), en_terms=("quantum sensors",), domain_tags=(),
                                  used_fallback=False)
        collection = CollectionView(collection_id="c-1", status="COMPLETED", documents_total=40, is_terminal=True)
        analysis = dataclasses.replace(_instance(AnalysisView), status="COMPLETED")
        job = types.SimpleNamespace(job_id="j-1", attempt=1, created_at=datetime(2026, 10, 3, tzinfo=UTC),
                                    query_text="квантовые сенсоры", requested_top_n=10, collection_id="", analysis_id="",
                                    status=JobStatus.QUEUED, cancel_requested=False, error_code=None)
        use_case = RunJob(_Jobs(), results, mock.Mock(), mock.Mock(), None, mock.Mock(), FakeClock(),
                          RunJobConfig(deadline_enabled=False))
        with mock.patch.object(run_job, "_bounded_expand", mock.AsyncMock(return_value=expansion)), \
             mock.patch.object(run_job, "run_collect", mock.AsyncMock(return_value=collection)), \
             mock.patch.object(run_job, "run_analyze", mock.AsyncMock(return_value=analysis)), \
             mock.patch.object(run_job, "run_narrate", mock.AsyncMock(return_value=NarrateOutcome())), \
             mock.patch.object(run_job, "build_stats", mock.Mock(return_value=mock.Mock())), \
             mock.patch.object(run_job, "decide_final_status",
                               mock.Mock(return_value=types.SimpleNamespace(status=JobStatus.COMPLETED, error_message=None))):
            await use_case.execute(job, "worker-1")
        traces = results.traces["j-1"]
        self.assertEqual(set(traces), {"job", "expand", "collect", "analyze"})
        self.assertEqual(traces["expand"]["en_terms"], ["quantum sensors"])
        self.assertEqual(traces["collect"]["documents_total"], 40)
        self.assertIn("quantum sensors", traces["analyze"]["query_text"])
        self.assertFalse(traces["job"]["deadline_enabled"])

    async def test_trace_failure_is_logged_not_raised(self) -> None:
        results = mock.Mock()
        results.save_trace = mock.AsyncMock(side_effect=RuntimeError("база недоступна"))
        use_case = RunJob(_Jobs(), results, mock.Mock(), mock.Mock(), None, mock.Mock(), FakeClock(), RunJobConfig())
        await use_case._trace("j-1", "expand", {"a": 1})  # noqa: SLF001 - проверка устойчивости записи трассы
        results.save_trace.assert_awaited_once()

    def test_json_safe_replaces_values_postgres_rejects(self) -> None:
        from ws_common.jsonsafe import json_safe  # noqa: PLC0415

        payload = {"p": float("nan"), "q": float("inf"), "t": (1, 2), "s": JobStatus.QUEUED, "n": None, "ok": 0.5}
        self.assertEqual(json_safe(payload), {"p": None, "q": None, "t": [1, 2], "s": JobStatus.QUEUED.value, "n": None,
                                              "ok": 0.5})

    def test_plain_serializes_dates_and_tuples(self) -> None:
        payload = plain(ExpansionView(ru_terms=("а",), en_terms=(), domain_tags=(), used_fallback=True))
        self.assertEqual(payload, {"ru_terms": ["а"], "en_terms": [], "domain_tags": [], "used_fallback": True,
                                   "technologies": []})


if __name__ == "__main__":
    unittest.main()
