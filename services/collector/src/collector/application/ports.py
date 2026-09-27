"""Порты прикладного слоя (`typing.Protocol`): хранилища, источники, HTTP, лимитер.

Реализации живут в `adapters/outbound`. Домен и use cases зависят только от этих протоколов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from collector.application.dto import (
    AdapterDelta,
    BatchItem,
    BatchOutcome,
    CollectionDraft,
    EncyclopediaHit,
    LeaseState,
)
from collector.domain.entities import AdapterRun, Collection, Document, RawDocument
from collector.domain.values import CollectionLimits, OperationStatus, SearchTerms, SourceKey


class HttpError(Exception):
    """Базовая ошибка HTTP-порта."""


class HttpStatusError(HttpError):
    """Ответ с кодом ≥ 400; `retry_after` — секунды из заголовка `Retry-After`, если он был."""

    def __init__(self, status: int, message: str, retry_after: float | None = None,
                 cdn_headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.cdn_headers = cdn_headers or {}


class HttpTimeoutError(HttpError):
    """Истёк таймаут соединения или чтения."""


class HttpTransportError(HttpError):
    """Сетевая ошибка, отличная от таймаута (DNS, TLS, разрыв соединения)."""


class HttpPayloadError(HttpError):
    """Ответ не разобран: превышен лимит размера или нарушен формат."""


class HttpClient(Protocol):
    """Внешние HTTP-запросы адаптеров с таймаутами, ретраями и белым списком хостов (SSRF)."""

    async def get_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET с разбором JSON."""

    async def get_text(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """GET с возвратом текста (XML/Atom/RSS)."""

    async def post_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST с JSON-телом и разбором JSON-ответа (поисковые API с параметрами в теле запроса)."""


class RateLimiter(Protocol):
    """Token bucket на адаптер (§13.4 ТЗ)."""

    async def acquire(self, source_key: SourceKey) -> None:
        """Ожидает разрешения на один запрос к источнику."""

    async def penalize(self, source_key: SourceKey, retry_after: float | None) -> None:
        """Реакция на 429: пауза `Retry-After` и временное снижение лимита."""


class SourceAdapter(Protocol):
    """Адаптер источника: выдаёт документы по поисковым фразам, не выбрасывая исключений наружу."""

    key: SourceKey
    raw_meta_keys: frozenset[str]

    @property
    def http_requests(self) -> int:
        """Число HTTP-запросов за время жизни экземпляра; запросы одного сбора считает RequestAccounting."""

    def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Итератор документов; `deadline` — монотонное время окончания бюджета."""


class EncyclopediaProbe(Protocol):
    """Проверка статьи Wikipedia (используется `CheckEncyclopedia`)."""

    async def probe(self, title: str, language_code: str) -> EncyclopediaHit:
        """Наличие статьи, ссылка, просмотры за 30 дней и дата создания."""

    @property
    def http_requests(self) -> int:
        """Число выполненных HTTP-запросов."""


class CollectionRepository(Protocol):
    """Хранилище коллекций (таблица `collections`)."""

    async def create_if_absent(self, draft: CollectionDraft) -> tuple[Collection, bool]:
        """Создаёт коллекцию и запуски адаптеров одной транзакцией.

        При конфликте по `idempotency_key` возвращает существующую коллекцию и True.
        """

    async def get(self, collection_id: str) -> Collection | None:
        """Коллекция по идентификатору."""

    async def count_pending(self) -> int:
        """Число коллекций в статусах PENDING/RUNNING (для RESOURCE_EXHAUSTED)."""

    async def claim_next(self, owner: str, lease_seconds: int) -> Collection | None:
        """Захватывает следующую коллекцию очереди (`FOR UPDATE SKIP LOCKED`) и ставит RUNNING."""

    async def heartbeat(self, collection_id: str, owner: str, lease_seconds: int) -> LeaseState:
        """Продлевает аренду; сообщает, жива ли аренда и запрошена ли отмена."""

    async def request_cancel(self, collection_id: str) -> OperationStatus | None:
        """Ставит `cancel_requested`; для PENDING сразу переводит в CANCELLED. Возвращает фактический статус."""

    async def finish(
        self,
        collection_id: str,
        owner: str,
        status: OperationStatus,
        *,
        error_code: str,
        error_message: str,
        finished_at: datetime,
    ) -> bool:
        """Терминальное завершение коллекции владельцем аренды."""

    async def release_expired_leases(self) -> int:
        """Возвращает коллекции с истёкшей арендой в PENDING; результат — число строк."""


class DocumentRepository(Protocol):
    """Хранилище документов и связей с коллекциями."""

    async def persist_batch(
        self,
        collection_id: str,
        items: Sequence[BatchItem],
        deltas: Mapping[SourceKey, AdapterDelta],
    ) -> BatchOutcome:
        """Записывает партию документов, связи и счётчики в одной транзакции."""

    async def get_many(self, document_ids: Sequence[str]) -> list[Document]:
        """Документы по идентификаторам (отсутствующие просто не возвращаются)."""

    async def page(
        self, collection_id: str, after: tuple[int, str] | None, limit: int
    ) -> list[tuple[int, Document]]:
        """Keyset-страница документов коллекции по `(relevance_rank, document_id)`."""

    async def purge_orphans(self, older_than: datetime, batch_size: int) -> int:
        """Удаляет документы вне коллекций старше указанной даты; возвращает число удалённых."""


class AdapterRunRepository(Protocol):
    """Хранилище запусков адаптеров (таблица `adapter_runs`)."""

    async def save(self, collection_id: str, run: AdapterRun) -> None:
        """Сохраняет статус, коды и счётчики запуска адаптера."""

    async def list_runs(self, collection_id: str) -> list[AdapterRun]:
        """Все запуски адаптеров коллекции."""


class EncyclopediaCacheRepository(Protocol):
    """Кеш результатов Wikipedia (таблица `encyclopedia_cache`)."""

    async def get_many(
        self, language_code: str, title_norms: Sequence[str], fresh_after: datetime
    ) -> dict[str, EncyclopediaHit]:
        """Свежие записи кеша по нормализованным названиям."""

    async def put(self, language_code: str, title_norm: str, hit: EncyclopediaHit) -> None:
        """Сохраняет результат проверки."""

    async def purge_older_than(self, moment: datetime) -> int:
        """Удаляет устаревшие записи кеша."""


class MetricsSink(Protocol):
    """Приём метрик сбора (реализация Prometheus — в `adapters/outbound/metrics.py`)."""

    def documents_new(self, source_key: SourceKey, count: int) -> None:
        """Число новых документов адаптера."""

    def adapter_finished(self, source_key: SourceKey, status: OperationStatus) -> None:
        """Завершение запуска адаптера."""

    def collection_finished(self, mode: str, status: OperationStatus, duration_seconds: float) -> None:
        """Завершение коллекции."""


class NullMetrics:
    """Метрики отключены (тесты и сценарии без экспортера)."""

    def documents_new(self, source_key: SourceKey, count: int) -> None:
        """Ничего не делает."""

    def adapter_finished(self, source_key: SourceKey, status: OperationStatus) -> None:
        """Ничего не делает."""

    def collection_finished(self, mode: str, status: OperationStatus, duration_seconds: float) -> None:
        """Ничего не делает."""
