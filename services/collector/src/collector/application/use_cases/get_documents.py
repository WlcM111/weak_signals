"""Сценарий GetDocuments: пакетное чтение документов по идентификаторам."""

from __future__ import annotations

from collections.abc import Sequence

from collector.application.dto import DocumentsResult
from collector.application.ports import DocumentRepository


class GetDocuments:
    """Возвращает найденные документы и список отсутствующих идентификаторов."""

    def __init__(self, documents: DocumentRepository) -> None:
        self._documents = documents

    async def execute(self, document_ids: Sequence[str]) -> DocumentsResult:
        """Порядок документов не гарантирован; отсутствующие перечисляются отдельно."""
        found = await self._documents.get_many(tuple(document_ids))
        found_ids = {document.document_id for document in found}
        missing = tuple(document_id for document_id in document_ids if document_id not in found_ids)
        return DocumentsResult(documents=tuple(found), missing_document_ids=missing)
