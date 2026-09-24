"""Сценарий PurgeDocuments: ретенция документов и кеша энциклопедии (§11.9 ТЗ)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from collector.application.ports import DocumentRepository, EncyclopediaCacheRepository
from ws_common.clock import Clock
from ws_common.logging import get_logger

ENCYCLOPEDIA_CACHE_RETENTION_DAYS = 30
PURGE_BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Итог одного прохода ретенции."""

    documents_deleted: int
    cache_entries_deleted: int


class PurgeDocuments:
    """Удаляет «осиротевшие» документы старше `retention_days` и устаревшие записи кеша."""

    def __init__(
        self,
        documents: DocumentRepository,
        cache: EncyclopediaCacheRepository,
        clock: Clock,
        retention_days: int,
        max_batches: int = 20,
    ) -> None:
        self._documents = documents
        self._cache = cache
        self._clock = clock
        self._retention_days = retention_days
        self._max_batches = max_batches
        self._log = get_logger("collector.purge")

    async def execute(self) -> PurgeResult:
        """Удаление батчами по 1000 строк, не более `max_batches` за проход."""
        now = self._clock.now()
        threshold = now - timedelta(days=self._retention_days)
        deleted = 0
        for _ in range(self._max_batches):
            batch = await self._documents.purge_orphans(threshold, PURGE_BATCH_SIZE)
            deleted += batch
            if batch < PURGE_BATCH_SIZE:
                break
        cache_deleted = await self._cache.purge_older_than(
            now - timedelta(days=ENCYCLOPEDIA_CACHE_RETENTION_DAYS)
        )
        if deleted or cache_deleted:
            self._log.info("retention.purged", documents=deleted, cache_entries=cache_deleted)
        return PurgeResult(documents_deleted=deleted, cache_entries_deleted=cache_deleted)
