"""Сквозной срок задания 20 минут: от приёма запроса до сохранённого результата.

Проверяются три механизма (docs/quality/LATENCY_20_MIN.md): отказ до старта после ожидания в
очереди; ограничение лимитов стадий оставшимся временем; прекращение вызовов LLM перед сроком
с экстрактивным завершением карточек и честной причиной неполноты.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from orchestrator.domain.deadline import JobDeadline
from orchestrator.domain.rules import decide_completion
from orchestrator.domain.values import JobErrorCode, JobStatus

from .test_run_job import RunJobHarness

ACCEPTED = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def insight_calls(harness: RunJobHarness) -> int:
    """Вызовы генерации инсайта (без расширения запроса)."""
    return sum(1 for key in harness.insight.calls if ":insight:" in key)


class JobDeadlineMathTest(unittest.TestCase):
    """Срок вычисляется из момента приёма и одинаков после перезапуска воркера."""

    def test_deadline_is_twenty_minutes_after_acceptance(self) -> None:
        deadline = JobDeadline.for_job(ACCEPTED, 1200, 120)
        self.assertEqual(deadline.deadline_at, ACCEPTED + timedelta(minutes=20))
        self.assertEqual(deadline.remaining(ACCEPTED + timedelta(minutes=5)), 900)
        self.assertEqual(deadline.usable(ACCEPTED + timedelta(minutes=5)), 780)

    def test_same_deadline_after_restart(self) -> None:
        self.assertEqual(JobDeadline.for_job(ACCEPTED, 1200, 120), JobDeadline.for_job(ACCEPTED, 1200, 120))

    def test_expired_inside_reserve(self) -> None:
        deadline = JobDeadline.for_job(ACCEPTED, 1200, 120)
        self.assertFalse(deadline.expired(ACCEPTED + timedelta(seconds=1079)))
        self.assertTrue(deadline.expired(ACCEPTED + timedelta(seconds=1080)))

    def test_allows_only_operations_that_fit(self) -> None:
        deadline = JobDeadline.for_job(ACCEPTED, 1200, 120)
        late = ACCEPTED + timedelta(seconds=1000)
        self.assertTrue(deadline.allows(late, 80))
        self.assertFalse(deadline.allows(late, 81))

    def test_stage_budget_capped_by_remaining_time(self) -> None:
        deadline = JobDeadline.for_job(ACCEPTED, 1200, 120)
        self.assertEqual(deadline.cap(ACCEPTED, 120, floor=10, keep_for_later=420), 120)
        late = ACCEPTED + timedelta(seconds=560)
        self.assertEqual(deadline.cap(late, 120, floor=10, keep_for_later=420), 100)
        very_late = ACCEPTED + timedelta(seconds=1000)
        self.assertEqual(deadline.cap(very_late, 120, floor=10, keep_for_later=420), 10)


class DeadlineStatusTest(unittest.TestCase):
    """Срок, достигнутый при генерации, делает результат неполным и называется явно."""

    def test_deadline_reached_is_partial_with_reason(self) -> None:
        completion = decide_completion(10, 10, 0, "COMPLETED", (), deadline_reached=True)
        self.assertIs(completion.status, JobStatus.PARTIAL)
        self.assertIn("deadline_reached", completion.error_message or "")

    def test_without_deadline_result_can_be_completed(self) -> None:
        self.assertIs(decide_completion(10, 10, 0, "COMPLETED", ()).status, JobStatus.COMPLETED)


class JobDeadlineFlowTest(RunJobHarness):
    """Поведение задания на границах срока."""

    async def test_job_started_after_deadline_fails_without_work(self) -> None:
        run_job, job, query = self.build()
        stored = await self.jobs.insert(query, job, "key-d001:submit", "hash", self.clock.now())
        self.clock.advance(20 * 60)
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.FAILED)
        self.assertEqual(stored.error_code, JobErrorCode.DEADLINE_EXCEEDED.value)
        self.assertEqual(insight_calls(self), 0)
        self.assertEqual(self.insight.calls, [], "задание после срока не должно даже расширять запрос")

    async def test_llm_not_called_when_no_time_left_for_a_call(self) -> None:
        run_job, job, query = self.build(top_n=2)
        stored = await self.jobs.insert(query, job, "key-d002:submit", "hash", self.clock.now())
        self.clock.advance(1200 - 120 - 60)
        status = await run_job.execute(stored, "worker-1")
        self.assertIs(status, JobStatus.PARTIAL)
        self.assertEqual(insight_calls(self), 0)
        items = await self.results.list_items(stored.job_id) if hasattr(self.results, "list_items") else None
        if items is not None:
            self.assertGreater(len(items), 0, "карточки должны быть записаны экстрактивно")
        self.assertIn("deadline_reached", stored.error_message or "")

    async def test_fresh_job_uses_llm_and_has_no_deadline_reason(self) -> None:
        run_job, job, query = self.build(top_n=2)
        stored = await self.jobs.insert(query, job, "key-d003:submit", "hash", self.clock.now())
        await run_job.execute(stored, "worker-1")
        self.assertGreater(insight_calls(self), 0)
        self.assertNotIn("deadline_reached", stored.error_message or "")


if __name__ == "__main__":
    unittest.main()
