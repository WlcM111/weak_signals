"""Регрессионные тесты исправлений аудита: API-ключ, параметры запроса, аренда, параллельность воркера."""

from __future__ import annotations

import asyncio
import time
import types
import unittest

from orchestrator.application.use_cases.run_job import CancelRequested, RunJobConfig, _JobControl
from orchestrator.application.use_cases.worker import WorkerLoop
from orchestrator.application.validation import check_api_key, validate_page_size, validate_top_n
from orchestrator.domain.errors import LeaseLost, ValidationError


class ApiKeyTest(unittest.TestCase):
    def test_non_ascii_key_is_rejected_without_exception(self) -> None:
        # значение-комментарий из .env: раньше TypeError → HTTP 500 на каждом запросе
        self.assertFalse(check_api_key("# пусто = аутентификация отключена", "secret"))
        self.assertTrue(check_api_key("# пусто", "# пусто"))
        self.assertFalse(check_api_key(None, "secret"))
        self.assertTrue(check_api_key(None, ""))


class QueryParameterTest(unittest.TestCase):
    def test_numeric_strings_are_accepted(self) -> None:
        self.assertEqual(validate_page_size("20"), 20)
        self.assertEqual(validate_top_n("15"), 15)
        self.assertEqual(validate_page_size(None), 20)
        self.assertEqual(validate_top_n(""), 15)

    def test_non_numeric_value_is_validation_error(self) -> None:
        for call in (lambda: validate_page_size("abc"), lambda: validate_top_n("1.5"), lambda: validate_page_size("500")):
            with self.assertRaises(ValidationError):
                call()


class _Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self):  # noqa: ANN201
        return None


class _Jobs:
    def __init__(self, alive: bool = True, cancel_after: int = 0) -> None:
        self.beats = 0
        self.alive = alive
        self.cancel_after = cancel_after

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> tuple[bool, bool]:
        self.beats += 1
        return self.alive, bool(self.cancel_after and self.beats >= self.cancel_after)


class KeepaliveTest(unittest.IsolatedAsyncioTestCase):
    def _control(self, jobs: _Jobs) -> _JobControl:
        job = types.SimpleNamespace(job_id="job-1", cancel_requested=False)
        return _JobControl(jobs, job, "worker-1", _Clock(), RunJobConfig(heartbeat_seconds=0.02, lease_seconds=60))

    async def test_lease_is_extended_during_long_call(self) -> None:
        jobs = _Jobs()
        control = self._control(jobs)
        task = asyncio.create_task(control.keepalive())
        await asyncio.sleep(0.15)  # «вызов LLM» без единого check()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(jobs.beats, 3)
        await control.check()  # аренда жива — исключений нет

    async def test_lost_lease_and_cancel_are_reported_by_check(self) -> None:
        lost = self._control(_Jobs(alive=False))
        await asyncio.wait_for(lost.keepalive(), timeout=1)
        with self.assertRaises(LeaseLost):
            await lost.check()
        cancelled = self._control(_Jobs(cancel_after=2))
        await asyncio.wait_for(cancelled.keepalive(), timeout=1)
        with self.assertRaises(CancelRequested):
            await cancelled.check()


class _Queue:
    def __init__(self, jobs: int, fail_first: int = 0) -> None:
        self.remaining = jobs
        self.fail_first = fail_first
        self.calls = 0
        self.stop: asyncio.Event | None = None

    async def claim_next(self, worker_id, lease_seconds, now):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("БД недоступна")
        if self.remaining <= 0:
            if self.stop is not None:
                self.stop.set()  # очередь пуста: тест завершает цикл без фиксированных пауз
            return None
        self.remaining -= 1
        return types.SimpleNamespace(job_id=f"job-{self.remaining}")


class _RunJob:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.done = 0

    async def execute(self, job, worker_id):  # noqa: ANN001, ANN201
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        self.done += 1


class WorkerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, queue: _Queue, run_job: _RunJob, concurrency: int) -> None:
        loop = WorkerLoop(queue, run_job, _Clock(), "w", lease_seconds=60, poll_seconds=0.01, concurrency=concurrency)
        queue.stop = asyncio.Event()
        await asyncio.wait_for(loop.run_forever(queue.stop), timeout=5)

    async def test_jobs_run_in_parallel_slots(self) -> None:
        run_job = _RunJob()
        await self._run(_Queue(jobs=4), run_job, concurrency=2)
        self.assertEqual(run_job.done, 4)
        self.assertEqual(run_job.peak, 2)

    async def test_claim_failure_does_not_stop_worker(self) -> None:
        run_job = _RunJob()
        await self._run(_Queue(jobs=1, fail_first=2), run_job, concurrency=1)
        self.assertEqual(run_job.done, 1)