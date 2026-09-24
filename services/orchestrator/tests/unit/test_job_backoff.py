"""Отложенный повтор задания: временная недоступность соседнего сервиса не губит задание."""

from __future__ import annotations

import unittest
from datetime import timedelta

from orchestrator.application.use_cases.run_job import POSTPONE_SECONDS
from orchestrator.domain.errors import UpstreamUnavailable
from orchestrator.domain.values import JobStatus

from .test_run_job import RunJobHarness


class WarmingAnalyzer:
    """Дублёр analyzer, который недоступен первые `failures` вызовов — как при прогреве модели."""

    def __init__(self, inner: object, failures: int = 1) -> None:
        self._inner = inner
        self.failures = failures
        self.calls = 0

    async def start_analysis(self, *args: object, **kwargs: object) -> str:
        """Запуск анализа: пока сервис поднимается, отвечает недоступностью."""
        self.calls += 1
        if self.calls <= self.failures:
            raise UpstreamUnavailable("StartAnalysis: UNAVAILABLE")
        return await self._inner.start_analysis(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class PostponeDelayTest(RunJobHarness):
    """Отложенное задание не захватывается раньше срока."""

    async def test_job_is_not_claimable_before_delay(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-b001:submit", "hash", self.clock.now())
        await self.jobs.transition(stored, JobStatus.COLLECTING, "worker-1", "CLAIMED")
        self.assertTrue(await self.jobs.postpone(stored, POSTPONE_SECONDS, "worker-1", "UPSTREAM_UNAVAILABLE"))
        self.assertIsNone(await self.jobs.claim_next("worker-1", 60, self.clock.now()))
        self.clock.advance(POSTPONE_SECONDS - 1)
        self.assertIsNone(await self.jobs.claim_next("worker-1", 60, self.clock.now()))
        self.clock.advance(2)
        claimed = await self.jobs.claim_next("worker-1", 60, self.clock.now())
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.job_id, stored.job_id)

    async def test_attempt_is_not_spent_by_postponement(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-b002:submit", "hash", self.clock.now())
        attempt_before = stored.attempt
        await self.jobs.transition(stored, JobStatus.COLLECTING, "worker-1", "CLAIMED")
        await self.jobs.postpone(stored, POSTPONE_SECONDS, "worker-1", "UPSTREAM_UNAVAILABLE")
        self.clock.advance(POSTPONE_SECONDS + 1)
        claimed = await self.jobs.claim_next("worker-1", 60, self.clock.now())
        assert claimed is not None
        self.assertEqual(claimed.attempt, attempt_before)


class AnalyzerWarmupTest(RunJobHarness):
    """Задание переживает перезапуск analyzer: откладывается и доходит до конца на повторе."""

    async def test_unavailable_analyzer_postpones_job(self) -> None:
        run_job, job, query = self.build()
        run_job._analyzer = WarmingAnalyzer(self.analyzer)  # noqa: SLF001 - подмена порта в тесте
        stored = await self.jobs.insert(query, job, "key-b003:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.QUEUED)
        self.assertEqual(self.jobs.postponements[stored.job_id], 1)
        self.assertIsNone(await self.jobs.claim_next("worker-1", 60, self.clock.now()))

    async def test_job_completes_after_analyzer_warms_up(self) -> None:
        run_job, job, query = self.build()
        warming = WarmingAnalyzer(self.analyzer)
        run_job._analyzer = warming  # noqa: SLF001 - подмена порта в тесте
        stored = await self.jobs.insert(query, job, "key-b004:submit", "hash", self.clock.now())
        self.assertIs(await run_job.execute(stored, "worker-1"), JobStatus.QUEUED)
        self.clock.advance(POSTPONE_SECONDS + 1)
        claimed = await self.jobs.claim_next("worker-1", 60, self.clock.now())
        assert claimed is not None
        status = await run_job.execute(claimed, "worker-1")
        self.assertIn(status, (JobStatus.COMPLETED, JobStatus.PARTIAL))
        self.assertEqual(warming.calls, 2)

    async def test_postpone_limit_still_fails_the_job(self) -> None:
        run_job, job, query = self.build()
        run_job._analyzer = WarmingAnalyzer(self.analyzer, failures=99)  # noqa: SLF001 - подмена порта
        stored = await self.jobs.insert(query, job, "key-b005:submit", "hash", self.clock.now())
        self.jobs.max_postponements = 0
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.FAILED)
        self.assertEqual(stored.error_code, "UPSTREAM_UNAVAILABLE")

    async def test_tolerance_window_covers_service_restart(self) -> None:
        run_job, job, query = self.build()
        run_job._analyzer = WarmingAnalyzer(self.analyzer, failures=99)  # noqa: SLF001 - подмена порта
        stored = await self.jobs.insert(query, job, "key-b006:submit", "hash", self.clock.now())
        attempts = 0
        while attempts < self.jobs.max_postponements:
            if await run_job.execute(stored, "worker-1") is not JobStatus.QUEUED:
                break
            attempts += 1
            self.clock.advance(POSTPONE_SECONDS + 1)
            claimed = await self.jobs.claim_next("worker-1", 60, self.clock.now())
            if claimed is None:
                break
        self.assertEqual(attempts, self.jobs.max_postponements)
        self.assertGreaterEqual(
            timedelta(seconds=attempts * POSTPONE_SECONDS), timedelta(minutes=5),
            "запаса по времени должно хватать на перезапуск сервиса с прогревом модели",
        )


if __name__ == "__main__":
    unittest.main()