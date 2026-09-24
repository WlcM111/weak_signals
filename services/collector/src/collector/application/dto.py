"""Объекты передачи данных между транспортом и use cases (без зависимости от proto)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from collector.domain.entities import AdapterRun, Document, DocumentDraft
from collector.domain.values import (
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
)


@dataclass(frozen=True, slots=True)
class StartCollectionCommand:
    """Запрос на запуск сбора (`StartCollectionRequest`)."""

    idempotency_key: str
    query_text: str
    terms: SearchTerms
    mode: CollectionMode
    limits: CollectionLimits
    sources: tuple[SourceKey, ...] = ()


@dataclass(frozen=True, slots=True)
class StartCollectionResult:
    """Результат запуска сбора (`StartCollectionResponse`)."""

    collection_id: str
    status: OperationStatus
    already_existed: bool


@dataclass(frozen=True, slots=True)
class CollectionView:
    """Состояние коллекции для ответа `GetCollection`."""

    collection_id: str
    status: OperationStatus
    documents_total: int
    sources_processed: int
    http_requests_total: int
    adapter_runs: tuple[AdapterRun, ...]
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str
    error_message: str


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """Страница документов коллекции (чанк стрима `StreamDocuments`)."""

    documents: tuple[Document, ...]
    next_page_token: str
    last: bool


@dataclass(frozen=True, slots=True)
class DocumentsResult:
    """Ответ `GetDocuments`: найденные документы и отсутствующие идентификаторы."""

    documents: tuple[Document, ...]
    missing_document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EncyclopediaHit:
    """Результат проверки одной статьи энциклопедии."""

    title: str
    exists: bool
    page_url: str = ""
    pageviews_30d: int = -1
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BatchItem:
    """Документ партии с присвоенным рангом релевантности."""

    draft: DocumentDraft
    relevance_rank: int


@dataclass(frozen=True, slots=True)
class AdapterDelta:
    """Прирост счётчиков адаптера, фиксируемый в одной транзакции с партией документов."""

    http_requests: int = 0
    documents_found: int = 0


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Результат записи партии: агрегаты коллекции после транзакции."""

    documents_total: int
    new_documents: int
    linked_documents: int
    new_by_source: dict[SourceKey, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeaseState:
    """Состояние аренды после heartbeat: жива ли аренда и запрошена ли отмена."""

    alive: bool
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class CollectionDraft:
    """Данные для вставки новой коллекции."""

    idempotency_key: str
    query_text: str
    mode: CollectionMode
    terms: SearchTerms
    limits: CollectionLimits
    sources: tuple[SourceKey, ...]
