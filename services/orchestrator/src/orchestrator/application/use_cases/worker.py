"""Сценарии воркера: захват задания, снятие истёкших аренд, очистка ключей идемпотентности."""

from __future__ import annotations

import asyncio

from orchestrator.application.ports import (
    IdempotencyRepository,
    JobRepository,
    MetricsSink,
    NullMetrics,
    ResultRepository,
)
from orchestrator.application.use_cases.run_job import RunJob
from orchestrator.domain.errors import LeaseLost
from ws_common.clock import Clock
from ws_common.logging import get_logger

LEASE_SWEEP_SECONDS = 15.0


class WorkerLoop:
    """Опрашивает очередь и выполняет задания по одному на слот параллельности."""

    def __init__(
        self,
        jobs: JobRepository,
        run_job: RunJob,
        clock: Clock,
        worker_id: str,
        lease_seconds: int,
        poll_seconds: float,
        concurrency: int = 1,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._jobs = jobs
        self._run_job = run_job
        self._clock = clock
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._concurrency = max(1, concurrency)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("orchestrator.worker")

    async def run_once(self) -> bool:
        """Берёт одно задание из очереди; False, если очередь пуста."""
        job = await self._jobs.claim_next(self._worker_id, self._lease_seconds, self._clock.now())
        if job is None:
            return False
        async with self._semaphore:
            try:
                await self._run_job.execute(job, self._worker_id)
            except LeaseLost:
                self._log.warning("job.lease_lost", job_id=job.job_id)
            except Exception as error:  # noqa: BLE001 - аренда истечёт, задание вернётся в очередь
                self._log.error("job.failed_unexpectedly", job_id=job.job_id, error=str(error))
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Цикл воркера до сигнала остановки: `concurrency` независимых слотов опрашивают очередь."""
        await asyncio.gather(*(self._slot(stop) for _ in range(self._concurrency)))

    async def _slot(self, stop: asyncio.Event) -> None:
        """Один слот параллельности; сбой захвата задания (БД недоступна) не останавливает воркер."""
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception as error:  # noqa: BLE001 - временный сбой БД: повтор после паузы
                self._log.error("worker.claim_failed", error=str(error))
                worked = False
            if not worked:
                await _wait(stop, self._poll_seconds)


class ReleaseExpiredLeases:
    """Фоновая задача: возврат заданий с истёкшей арендой в очередь."""

    def __init__(self, jobs: JobRepository, clock: Clock, interval: float = LEASE_SWEEP_SECONDS) -> None:
        self._jobs = jobs
        self._clock = clock
        self._interval = interval
        self._log = get_logger("orchestrator.maintenance")

    async def run_once(self) -> int:
        """Один проход снятия аренд; возвращает число возвращённых заданий."""
        released = await self._jobs.release_expired_leases(self._clock.now())
        if released:
            self._log.warning("job.leases_released", count=released)
        return released

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Периодический проход до сигнала остановки."""
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as error:  # noqa: BLE001 - фоновая задача не останавливает сервис
                self._log.error("maintenance.failed", error=str(error))
            await _wait(stop, self._interval)


class Cleanup:
    """Фоновая задача: удаление истёкших ключей идемпотентности и публикация длины очереди."""

    def __init__(
        self,
        idempotency: IdempotencyRepository,
        jobs: JobRepository,
        results: ResultRepository,
        clock: Clock,
        interval_minutes: int = 60,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._idempotency = idempotency
        self._jobs = jobs
        self._results = results
        self._clock = clock
        self._interval = interval_minutes * 60
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("orchestrator.retention")

    async def run_once(self) -> int:
        """Удаляет истёкшие ключи; возвращает их число."""
        purged = await self._idempotency.purge_expired(self._clock.now())
        self._metrics.queue_pending(await self._jobs.count_pending())
        self._log.info("retention.run", purged_idempotency_keys=purged)
        return purged

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Периодическая очистка до сигнала остановки."""
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception as error:  # noqa: BLE001
                self._log.error("retention.failed", error=str(error))
            await _wait(stop, self._interval)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Ожидание с досрочным выходом по сигналу остановки."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return
