"""Воркер коллекций: захват заданий из очереди PostgreSQL и выполнение сбора (§9.2 ТЗ)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from collector.application.ports import CollectionRepository
from collector.application.use_cases.purge_documents import PurgeDocuments
from collector.application.use_cases.run_collection import RunCollection
from collector.domain.errors import LeaseLost
from collector.domain.values import OperationStatus
from ws_common.logging import get_logger

POLL_INTERVAL_SECONDS = 1.0
RETENTION_INTERVAL_SECONDS = 3600.0
LEASE_SWEEP_INTERVAL_SECONDS = 30.0


class CollectionWorker:
    """Опрашивает очередь коллекций и выполняет сбор; на процесс запускается несколько задач."""

    def __init__(
        self,
        collections: CollectionRepository,
        run_collection: RunCollection,
        owner: str,
        lease_seconds: int,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._collections = collections
        self._run_collection = run_collection
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._log = get_logger("collector.worker")

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Цикл воркера до сигнала остановки; сбой БД при захвате не завершает задачу воркера."""
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - временный сбой БД: повтор после паузы
                self._log.error("worker.claim_failed", error=str(exc))
                processed = False
            if not processed:
                await _wait_or_stop(stop, self._poll_interval)

    async def run_once(self) -> bool:
        """Обрабатывает одну коллекцию, если она есть в очереди."""
        collection = await self._collections.claim_next(self._owner, self._lease_seconds)
        if collection is None:
            return False
        try:
            await self._run_collection.execute(collection, self._owner)
        except LeaseLost:
            self._log.warning("collection.lease_lost", collection_id=collection.collection_id)
        except Exception as exc:  # noqa: BLE001 - непредвиденная ошибка: коллекция завершается отказом
            self._log.error(
                "collection.failed_unexpectedly", collection_id=collection.collection_id, error=str(exc)
            )
            await self._fail_poisoned(collection.collection_id, str(exc))
        return True

    async def _fail_poisoned(self, collection_id: str, reason: str) -> None:
        """Завершает коллекцию отказом после непредвиденной ошибки.

        Без этого аренда истекала, коллекция возвращалась в очередь и падала на том же месте
        бесконечно, а задание orchestrator ждало её до тайм-аута. Если отказ записать не удалось
        (БД недоступна), коллекцию по истечении аренды подхватит следующая попытка.
        """
        try:
            await self._collections.finish(
                collection_id,
                self._owner,
                OperationStatus.FAILED,
                error_code="INTERNAL_ERROR",
                error_message=f"непредвиденная ошибка сбора: {reason}"[:500],
                finished_at=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - запись отказа не должна ронять воркер
            self._log.error("collection.fail_record_failed", collection_id=collection_id, error=str(exc))


class MaintenanceWorker:
    """Фоновое обслуживание: снятие истёкших аренд и ретенция документов."""

    def __init__(
        self,
        collections: CollectionRepository,
        purge: PurgeDocuments,
        lease_sweep_interval: float = LEASE_SWEEP_INTERVAL_SECONDS,
        retention_interval: float = RETENTION_INTERVAL_SECONDS,
    ) -> None:
        self._collections = collections
        self._purge = purge
        self._lease_sweep_interval = lease_sweep_interval
        self._retention_interval = retention_interval
        self._log = get_logger("collector.maintenance")

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Периодические задачи до сигнала остановки."""
        elapsed = 0.0
        while not stop.is_set():
            try:
                released = await self._collections.release_expired_leases()
                if released:
                    self._log.warning("collection.leases_released", count=released)
                elapsed += self._lease_sweep_interval
                if elapsed >= self._retention_interval:
                    elapsed = 0.0
                    await self._purge.execute()
            except Exception as exc:  # noqa: BLE001 - фоновая задача не должна завершаться из-за сбоя БД
                self._log.error("maintenance.failed", error=str(exc))
            await _wait_or_stop(stop, self._lease_sweep_interval)


async def _wait_or_stop(stop: asyncio.Event, timeout: float) -> None:
    """Пауза, прерываемая сигналом остановки."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return
