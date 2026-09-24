"""Сценарий GetCollection: статус, прогресс и запуски адаптеров."""

from __future__ import annotations

from collector.application.dto import CollectionView
from collector.application.ports import AdapterRunRepository, CollectionRepository
from collector.domain.errors import NotFoundError


class GetCollection:
    """Читает коллекцию и её запуски адаптеров."""

    def __init__(self, collections: CollectionRepository, adapter_runs: AdapterRunRepository) -> None:
        self._collections = collections
        self._adapter_runs = adapter_runs

    async def execute(self, collection_id: str) -> CollectionView:
        """Состояние коллекции; NOT_FOUND, если её нет."""
        collection = await self._collections.get(collection_id)
        if collection is None:
            raise NotFoundError(f"коллекция {collection_id} не найдена")
        runs = await self._adapter_runs.list_runs(collection_id)
        return CollectionView(
            collection_id=collection.collection_id,
            status=collection.status,
            documents_total=collection.documents_total,
            sources_processed=sum(1 for run in runs if run.status.is_terminal),
            http_requests_total=collection.http_requests_total,
            adapter_runs=tuple(runs),
            started_at=collection.started_at,
            finished_at=collection.finished_at,
            error_code=collection.error_code,
            error_message=collection.error_message,
        )
