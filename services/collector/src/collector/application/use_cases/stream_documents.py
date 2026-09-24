"""Сценарий StreamDocuments: потоковая keyset-выдача документов завершённой коллекции."""

from __future__ import annotations

from collections.abc import AsyncIterator

from collector.application.dto import DocumentPage
from collector.application.ports import CollectionRepository, DocumentRepository
from collector.domain.errors import NotFoundError, PreconditionFailedError
from collector.domain.rules import encode_page_token


class StreamDocuments:
    """Отдаёт документы чанками по `(relevance_rank, document_id)`; возобновляется по `page_token`."""

    def __init__(self, collections: CollectionRepository, documents: DocumentRepository) -> None:
        self._collections = collections
        self._documents = documents

    async def execute(
        self, collection_id: str, chunk_size: int, after: tuple[int, str] | None
    ) -> AsyncIterator[DocumentPage]:
        """Асинхронный итератор чанков; последний чанк помечен `last=True`."""
        collection = await self._collections.get(collection_id)
        if collection is None:
            raise NotFoundError(f"коллекция {collection_id} не найдена")
        if not collection.status.is_terminal:
            raise PreconditionFailedError(
                f"коллекция в статусе {collection.status.value}: документы доступны только после завершения",
                "COLLECTION_NOT_TERMINAL",
            )
        cursor = after
        while True:
            rows = await self._documents.page(collection_id, cursor, chunk_size)
            if not rows:
                yield DocumentPage(documents=(), next_page_token="", last=True)
                return
            last_rank, last_document = rows[-1]
            cursor = (last_rank, last_document.document_id)
            is_last = len(rows) < chunk_size
            yield DocumentPage(
                documents=tuple(document for _, document in rows),
                next_page_token="" if is_last else encode_page_token(last_rank, last_document.document_id),
                last=is_last,
            )
            if is_last:
                return
