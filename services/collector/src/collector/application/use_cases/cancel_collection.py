"""Сценарий CancelCollection: кооперативная идемпотентная отмена."""

from __future__ import annotations

from collector.application.ports import CollectionRepository
from collector.domain.errors import NotFoundError
from collector.domain.values import OperationStatus
from ws_common.logging import get_logger


class CancelCollection:
    """Ставит признак отмены; повторный вызов возвращает фактический статус без ошибки."""

    def __init__(self, collections: CollectionRepository) -> None:
        self._collections = collections
        self._log = get_logger("collector.cancel_collection")

    async def execute(self, collection_id: str, reason: str) -> OperationStatus:
        """Возвращает CANCELLED либо уже достигнутый терминальный статус."""
        status = await self._collections.request_cancel(collection_id)
        if status is None:
            raise NotFoundError(f"коллекция {collection_id} не найдена")
        self._log.info("collection.cancel_requested", collection_id=collection_id, status=status.value, reason=reason)
        return status
