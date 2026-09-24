"""Сценарий StartCollection: идемпотентный запуск сбора."""

from __future__ import annotations

from collections.abc import Sequence

from collector.application.dto import CollectionDraft, StartCollectionCommand, StartCollectionResult
from collector.application.ports import CollectionRepository
from collector.domain.errors import ResourceExhaustedError
from collector.domain.values import SourceKey
from ws_common.logging import get_logger


class StartCollection:
    """Создаёт коллекцию и строки запусков адаптеров; повтор по ключу возвращает существующую."""

    def __init__(
        self,
        collections: CollectionRepository,
        available_sources: Sequence[SourceKey],
        max_pending: int,
    ) -> None:
        self._collections = collections
        self._available_sources = tuple(available_sources)
        self._max_pending = max_pending
        self._log = get_logger("collector.start_collection")

    async def execute(self, command: StartCollectionCommand) -> StartCollectionResult:
        """Валидированная команда → коллекция в статусе PENDING (или уже существующая)."""
        sources = command.sources or self._available_sources
        if not sources:
            raise ResourceExhaustedError("нет доступных адаптеров источников", "NO_SOURCES_ENABLED")
        pending = await self._collections.count_pending()
        if pending >= self._max_pending:
            raise ResourceExhaustedError(
                f"очередь коллекций заполнена ({pending} ≥ {self._max_pending})", "QUEUE_FULL"
            )
        collection, already_existed = await self._collections.create_if_absent(
            CollectionDraft(
                idempotency_key=command.idempotency_key,
                query_text=command.query_text,
                mode=command.mode,
                terms=command.terms,
                limits=command.limits,
                sources=tuple(sources),
            )
        )
        if not already_existed:
            self._log.info(
                "collection.accepted",
                collection_id=collection.collection_id,
                mode=command.mode.value,
                terms_count=command.terms.total,
                sources=[source.value for source in sources],
            )
        return StartCollectionResult(
            collection_id=collection.collection_id,
            status=collection.status,
            already_existed=already_existed,
        )
