"""Сквозное выполнение задания воркером: успех, отмена, потеря аренды, откладывание."""

from __future__ import annotations

import asyncio
import unittest

from orchestrator.application.stages.analyze import AnalyzeConfig
from orchestrator.application.stages.collect import CollectConfig
from orchestrator.application.stages.expand import Glossary
from orchestrator.application.stages.narrate import NarrateConfig
from orchestrator.application.use_cases.run_job import RunJob, RunJobConfig
from orchestrator.application.use_cases.worker import ReleaseExpiredLeases, WorkerLoop
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.errors import LeaseLost
from orchestrator.domain.values import Decision, JobStatus

from ..fakes import (
    FakeAnalyzer,
    FakeClock,
    FakeCollector,
    FakeInsight,
    InMemoryJobRepository,
    InMemoryResultRepository,
    make_candidate,
    make_document,
    new_id,
)

CONFIG = RunJobConfig(
    lease_seconds=60,
    heartbeat_seconds=0,
    collect=CollectConfig(poll_seconds=1.0),
    analyze=AnalyzeConfig(poll_seconds=1.0),
    narrate=NarrateConfig(top_n=15),
)


class RunJobHarness(unittest.IsolatedAsyncioTestCase):
    """Сборка задания и зависимостей для прогонов."""

    def build(self, *, collector=None, analyzer=None, insight=None, top_n=2):  # noqa: ANN001, ANN201
        """Создаёт репозитории, задание в очереди и сценарий выполнения."""
        clock = FakeClock()
        jobs = InMemoryJobRepository(clock)
        results = InMemoryResultRepository(jobs)
        documents = {make_document(index).document_id: make_document(index) for index in (1, 2)}
        collector = collector or FakeCollector(statuses=["COMPLETED"], documents=documents)
        analyzer = analyzer or FakeAnalyzer(
            statuses=["COMPLETED"],
            candidates=[
                make_candidate(1, rank=1, score=0.92),
                make_candidate(2, rank=2, score=0.81),
                make_candidate(3, decision=Decision.MATURE, score=0.15),
            ],
        )
        query = Query.create(new_id(2), "слабые сигналы в ИИ", top_n)
        job = Job(job_id=new_id(1), query_id=query.query_id)
        asyncio.get_event_loop()
        self.clock = clock
        self.jobs = jobs
        self.results = results
        self.collector = collector
        self.analyzer = analyzer
        self.insight = insight if insight is not None else FakeInsight()
        run_job = RunJob(
            jobs=jobs,
            results=results,
            collector=collector,
            analyzer=analyzer,
            insight=self.insight,
            glossary=Glossary({"технологии": "technologies"}),
            clock=clock,
            config=CONFIG,
        )
        return run_job, job, query


class HappyPathTest(RunJobHarness):
    """Полный прогон QUEUED → COMPLETED."""

    async def asyncSetUp(self) -> None:
        self.run_job, job, query = self.build(top_n=2)
        self.job = await self.jobs.insert(query, job, "key-0001:submit", "hash", self.clock.now())
        self.status = await self.run_job.execute(self.job, "worker-1")

    async def test_completes(self) -> None:
        self.assertIs(self.status, JobStatus.COMPLETED)
        self.assertIs(self.job.status, JobStatus.COMPLETED)
        self.assertIsNotNone(self.job.finished_at)

    async def test_writes_top_n_items(self) -> None:
        items = self.results.items[self.job.job_id]
        self.assertEqual(len(items), 2)
        self.assertEqual([item.rank for item in items], [1, 2])

    async def test_records_excluded_with_reasons(self) -> None:
        excluded = self.results.excluded[self.job.job_id]
        self.assertEqual(len(excluded), 1)
        self.assertIs(excluded[0].decision, Decision.MATURE)
        self.assertTrue(excluded[0].decision_explanation_ru)

    async def test_stats_filled(self) -> None:
        stats = self.results.stats[self.job.job_id]
        self.assertEqual(stats.documents_collected, 120)
        self.assertEqual(stats.weak_signals_total, 2)
        self.assertEqual(stats.narratives_generated, 2)
        self.assertFalse(stats.expand_used_fallback)
        self.assertEqual(stats.model_version_id, "wsclf-2026.09.16-1")

    async def test_stage_sequence_recorded(self) -> None:
        transitions = [event[2] for event in self.jobs.events if event[0] == self.job.job_id]
        self.assertEqual(
            transitions, ["QUEUED", "COLLECTING", "ANALYZING", "NARRATING", "COMPLETED"]
        )

    async def test_references_saved(self) -> None:
        self.assertTrue(self.job.collection_id)
        self.assertTrue(self.job.analysis_id)

    async def test_items_carry_full_payload(self) -> None:
        item = self.results.items[self.job.job_id][0]
        for field in (
            item.title_ru, item.description_ru, item.advantage_ru,
            item.case_example_ru, item.explanation_ru, item.decision_explanation_ru,
        ):
            self.assertTrue(field)
        self.assertTrue(item.features)
        self.assertTrue(item.sources)
        self.assertTrue(all(source.summary_ru for source in item.sources))


class PartialPathTest(RunJobHarness):
    """Неполный результат даёт PARTIAL с расшифровкой причин."""

    async def test_partial_on_fallback_narratives(self) -> None:
        run_job, job, query = self.build(insight=FakeInsight(fail_generate=True), top_n=2)
        stored = await self.jobs.insert(query, job, "key-0002:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.PARTIAL)
        self.assertIn("fallback_narratives=2", stored.error_message)

    async def test_partial_when_fewer_items_than_requested(self) -> None:
        run_job, job, query = self.build(top_n=10)
        stored = await self.jobs.insert(query, job, "key-0003:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.PARTIAL)
        self.assertIn("found=2<10", stored.error_message)


class FailurePathTest(RunJobHarness):
    """Отказы стадий переводят задание в FAILED с кодом."""

    async def test_too_few_documents(self) -> None:
        collector = FakeCollector(statuses=["COMPLETED"], documents_total=3)
        run_job, job, query = self.build(collector=collector)
        stored = await self.jobs.insert(query, job, "key-0004:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.FAILED)
        self.assertEqual(stored.error_code, "TOO_FEW_DOCUMENTS")

    async def test_analysis_failure(self) -> None:
        analyzer = FakeAnalyzer(statuses=["FAILED"])
        run_job, job, query = self.build(analyzer=analyzer)
        stored = await self.jobs.insert(query, job, "key-0005:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.FAILED)
        self.assertEqual(stored.error_code, "ANALYSIS_FAILED")

    async def test_retryable_upstream_is_postponed(self) -> None:
        collector = FakeCollector(fail_start=True)
        run_job, job, query = self.build(collector=collector)
        stored = await self.jobs.insert(query, job, "key-0006:submit", "hash", self.clock.now())
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.QUEUED)
        self.assertEqual(self.jobs.postponements[stored.job_id], 1)

    async def test_postpone_limit_leads_to_failure(self) -> None:
        collector = FakeCollector(fail_start=True)
        run_job, job, query = self.build(collector=collector)
        stored = await self.jobs.insert(query, job, "key-0007:submit", "hash", self.clock.now())
        self.jobs.max_postponements = 0
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.FAILED)
        self.assertEqual(stored.error_code, "UPSTREAM_UNAVAILABLE")


class CancellationTest(RunJobHarness):
    """Отмена и потеря аренды."""

    async def test_cancel_during_collect(self) -> None:
        collector = FakeCollector(statuses=["RUNNING", "COMPLETED"])
        run_job, job, query = self.build(collector=collector)
        stored = await self.jobs.insert(query, job, "key-0008:submit", "hash", self.clock.now())
        stored.cancel_requested = True
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.CANCELLED)
        self.assertNotIn(stored.job_id, self.results.items)

    async def test_cancel_calls_upstream_cancel(self) -> None:
        collector = FakeCollector(statuses=["RUNNING", "RUNNING", "COMPLETED"])
        run_job, job, query = self.build(collector=collector)
        stored = await self.jobs.insert(query, job, "key-0009:submit", "hash", self.clock.now())

        beats = {"count": 0}

        async def cancel_after_collection_started(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            """Отмена приходит уже после запуска сбора — как при реальном нажатии «Отменить»."""
            beats["count"] += 1
            if beats["count"] <= 1:
                return True, False
            stored.cancel_requested = True
            return True, True

        self.jobs.heartbeat = cancel_after_collection_started  # type: ignore[assignment]
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.CANCELLED)
        self.assertEqual(collector.cancelled, [collector.collection_id])

    async def test_lease_lost_raises(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-0010:submit", "hash", self.clock.now())
        self.jobs.lease_alive = False
        with self.assertRaises(LeaseLost):
            await run_job.execute(stored, "worker-1")


class WorkerLoopTest(RunJobHarness):
    """Захват задания воркером и снятие истёкших аренд."""

    async def test_worker_claims_and_runs(self) -> None:
        run_job, job, query = self.build(top_n=2)
        await self.jobs.insert(query, job, "key-0011:submit", "hash", self.clock.now())
        loop = WorkerLoop(self.jobs, run_job, self.clock, "worker-1", 60, 0.1)
        self.assertTrue(await loop.run_once())
        self.assertIs(self.jobs.jobs[job.job_id].status, JobStatus.COMPLETED)
        self.assertFalse(await loop.run_once())

    async def test_second_worker_does_not_get_running_job(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-0012:submit", "hash", self.clock.now())
        first = await self.jobs.claim_next("worker-1", 60, self.clock.now())
        self.assertIsNotNone(first)
        stored.status = JobStatus.COLLECTING
        second = await self.jobs.claim_next("worker-2", 60, self.clock.now())
        self.assertIsNone(second)

    async def test_expired_lease_is_released_and_retried(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-0013:submit", "hash", self.clock.now())
        await self.jobs.claim_next("worker-1", 60, self.clock.now())
        stored.status = JobStatus.NARRATING
        self.clock.advance(120)
        released = await ReleaseExpiredLeases(self.jobs, self.clock).run_once()
        self.assertEqual(released, 1)
        self.assertIs(stored.status, JobStatus.QUEUED)
        self.assertEqual(stored.attempt, 1)
        claimed = await self.jobs.claim_next("worker-2", 60, self.clock.now())
        self.assertIsNotNone(claimed)


if __name__ == "__main__":
    unittest.main()
