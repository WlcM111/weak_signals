"""Тестовые замены портов collector: детерминированные часы и хранилища в памяти.

Используются только в тестах; рабочая сборка подключает адаптеры PostgreSQL и httpx.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from collector.application.dto import (
    AdapterDelta,
    BatchItem,
    BatchOutcome,
    CollectionDraft,
    EncyclopediaHit,
    LeaseState,
)
from collector.application.ports import (
    HttpError,
    HttpStatusError,
)
from collector.application.request_accounting import current_request_accounting
from collector.domain.entities import AdapterRun, Collection, Document, RawDocument
from collector.domain.values import (
    CollectionLimits,
    OperationStatus,
    SearchTerms,
    SourceKey,
)

BASE_TIME = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeClock:
    """Управляемые часы: время двигается только явным вызовом `advance`.

    `sleep` не сдвигает время (иначе фоновые задачи с длинными паузами мгновенно исчерпывали бы
    бюджеты сбора), а лишь записывает запрошенную длительность и уступает цикл событий.
    """

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self._now = start
        self._monotonic = 1000.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await asyncio.sleep(0)  # уступить цикл событий, не задерживая тест

    def advance(self, seconds: float) -> None:
        """Сдвигает виртуальное время вперёд."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class InMemoryCollectionRepository:
    """Хранилище коллекций в памяти с семантикой аренд и идемпотентности."""

    def __init__(self, clock: FakeClock, adapter_runs: InMemoryAdapterRunRepository) -> None:
        self._clock = clock
        self._adapter_runs = adapter_runs
        self.items: dict[str, Collection] = {}
        self._by_key: dict[str, str] = {}
        self._sequence = 0
        self.heartbeat_calls = 0

    async def create_if_absent(self, draft: CollectionDraft) -> tuple[Collection, bool]:
        existing_id = self._by_key.get(draft.idempotency_key)
        if existing_id is not None:
            return self.items[existing_id], True
        self._sequence += 1
        collection_id = f"00000000-0000-4000-8000-{self._sequence:012d}"
        collection = Collection(
            collection_id=collection_id,
            idempotency_key=draft.idempotency_key,
            query_text=draft.query_text,
            mode=draft.mode,
            terms=draft.terms,
            limits=draft.limits,
            created_at=self._clock.now(),
            sources=draft.sources,
        )
        self.items[collection_id] = collection
        self._by_key[draft.idempotency_key] = collection_id
        self._adapter_runs.seed(collection_id, draft.sources)
        return collection, False

    async def get(self, collection_id: str) -> Collection | None:
        return self.items.get(collection_id)

    async def count_pending(self) -> int:
        return sum(
            1
            for item in self.items.values()
            if item.status in (OperationStatus.PENDING, OperationStatus.RUNNING)
        )

    async def claim_next(self, owner: str, lease_seconds: int) -> Collection | None:
        for collection in self.items.values():
            if collection.status is OperationStatus.PENDING and not collection.cancel_requested:
                collection.transition_to(OperationStatus.RUNNING, self._clock.now())
                return collection
        return None

    async def heartbeat(self, collection_id: str, owner: str, lease_seconds: int) -> LeaseState:
        self.heartbeat_calls += 1
        collection = self.items.get(collection_id)
        if collection is None or collection.status is not OperationStatus.RUNNING:
            return LeaseState(alive=False, cancel_requested=False)
        return LeaseState(alive=True, cancel_requested=collection.cancel_requested)

    async def request_cancel(self, collection_id: str) -> OperationStatus | None:
        collection = self.items.get(collection_id)
        if collection is None:
            return None
        collection.cancel_requested = True
        if collection.status is OperationStatus.PENDING:
            collection.finish(OperationStatus.CANCELLED, self._clock.now(), "CANCELLED", "отменено до запуска")
        return collection.status

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
        collection = self.items.get(collection_id)
        if collection is None or collection.status is not OperationStatus.RUNNING:
            return False
        collection.finish(status, finished_at, error_code, error_message)
        return True

    async def release_expired_leases(self) -> int:
        return 0


class InMemoryAdapterRunRepository:
    """Запуски адаптеров в памяти."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[SourceKey, AdapterRun]] = {}

    def seed(self, collection_id: str, sources: Sequence[SourceKey]) -> None:
        """Создаёт строки PENDING (аналог транзакции создания коллекции)."""
        self.runs[collection_id] = {source: AdapterRun(source_key=source) for source in sources}

    async def save(self, collection_id: str, run: AdapterRun) -> None:
        self.runs.setdefault(collection_id, {})[run.source_key] = run

    async def list_runs(self, collection_id: str) -> list[AdapterRun]:
        # копии строк: настоящий репозиторий возвращает новые объекты, а не ссылки на хранимые
        return [replace(run) for run in self.runs.get(collection_id, {}).values()]


class InMemoryDocumentRepository:
    """Документы и связи с коллекциями в памяти; повторяет правила дедупликации хранилища."""

    def __init__(self, clock: FakeClock, collections: InMemoryCollectionRepository) -> None:
        self._clock = clock
        self._collections = collections
        self.documents: dict[str, Document] = {}
        self._by_url_hash: dict[str, str] = {}
        self._by_doi: dict[str, str] = {}
        self.links: dict[str, list[tuple[int, str]]] = {}
        self.batches: list[int] = []
        self.persisted: list[BatchItem] = []
        self.deltas: list[Mapping[SourceKey, AdapterDelta]] = []
        self._sequence = 0

    async def persist_batch(
        self,
        collection_id: str,
        items: Sequence[BatchItem],
        deltas: Mapping[SourceKey, AdapterDelta],
    ) -> BatchOutcome:
        self.batches.append(len(items))
        self.persisted.extend(items)
        self.deltas.append(dict(deltas))
        new_by_source: dict[SourceKey, int] = {}
        linked = 0
        new_documents = 0
        for item in items:
            draft = item.draft
            document_id = self._by_doi.get(draft.doi or "") or self._by_url_hash.get(draft.url_hash)
            is_new = document_id is None
            if document_id is None:
                self._sequence += 1
                document_id = f"11111111-0000-4000-8000-{self._sequence:012d}"
                self.documents[document_id] = Document(
                    document_id=document_id,
                    url=draft.url,
                    title=draft.title,
                    text=draft.text,
                    language_code=draft.language_code,
                    source_key=draft.source_key,
                    source_type=draft.source_type,
                    trust_level=draft.trust_level,
                    fetched_at=draft.fetched_at,
                    origin_domain=draft.origin_domain,
                    published_at=draft.published_at,
                    doi=draft.doi,
                    citation_count=draft.citation_count,
                    engagement_count=draft.engagement_count,
                )
                self._by_url_hash[draft.url_hash] = document_id
                if draft.doi:
                    self._by_doi[draft.doi] = document_id
            links = self.links.setdefault(collection_id, [])
            if any(existing_id == document_id for _, existing_id in links):
                continue
            links.append((item.relevance_rank, document_id))
            linked += 1
            if is_new:
                new_documents += 1
                new_by_source[draft.source_key] = new_by_source.get(draft.source_key, 0) + 1
        collection = self._collections.items.get(collection_id)
        if collection is not None:
            collection.documents_total += linked
            collection.http_requests_total += sum(delta.http_requests for delta in deltas.values())
        runs = self._collections._adapter_runs.runs.get(collection_id, {})  # noqa: SLF001 - тестовая замена
        for source_key, delta in deltas.items():
            run = runs.get(source_key)
            if run is not None:
                run.documents_new += new_by_source.get(source_key, 0)
        return BatchOutcome(
            documents_total=collection.documents_total if collection else linked,
            new_documents=new_documents,
            linked_documents=linked,
            new_by_source=new_by_source,
        )

    async def get_many(self, document_ids: Sequence[str]) -> list[Document]:
        return [self.documents[doc_id] for doc_id in document_ids if doc_id in self.documents]

    async def page(
        self, collection_id: str, after: tuple[int, str] | None, limit: int
    ) -> list[tuple[int, Document]]:
        rows = sorted(self.links.get(collection_id, []))
        if after is not None:
            rows = [row for row in rows if row > after]
        return [(rank, self.documents[document_id]) for rank, document_id in rows[:limit]]

    async def purge_orphans(self, older_than: datetime, batch_size: int) -> int:
        linked_ids = {doc_id for links in self.links.values() for _, doc_id in links}
        victims = [
            doc_id
            for doc_id, document in self.documents.items()
            if doc_id not in linked_ids and document.fetched_at < older_than
        ][:batch_size]
        for doc_id in victims:
            del self.documents[doc_id]
        return len(victims)


class InMemoryEncyclopediaCache:
    """Кеш энциклопедии в памяти с учётом TTL."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], tuple[EncyclopediaHit, datetime]] = {}
        self.writes = 0

    async def get_many(
        self, language_code: str, title_norms: Sequence[str], fresh_after: datetime
    ) -> dict[str, EncyclopediaHit]:
        found: dict[str, EncyclopediaHit] = {}
        for title_norm in title_norms:
            entry = self.entries.get((language_code, title_norm))
            if entry is not None and entry[1] >= fresh_after:
                found[title_norm] = entry[0]
        return found

    async def put(self, language_code: str, title_norm: str, hit: EncyclopediaHit) -> None:
        self.writes += 1
        self.entries[(language_code, title_norm)] = (hit, BASE_TIME)

    async def purge_older_than(self, moment: datetime) -> int:
        victims = [key for key, (_, checked) in self.entries.items() if checked < moment]
        for key in victims:
            del self.entries[key]
        return len(victims)


class FakeHttpClient:
    """HTTP-порт с заранее записанными ответами (VCR-кассеты `tests/fixtures/http`)."""

    def __init__(self, responses: Sequence[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, response: Any) -> None:
        """Добавляет очередной ответ (объект, строку или исключение)."""
        self.responses.append(response)

    async def get_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self._next(url, params, headers)

    async def get_text(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        return str(self._next(url, params, headers))

    async def post_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self._next(url, None, headers, json_body)

    def _next(
        self,
        url: str,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        call: dict[str, Any] = {"params": dict(params or {}), "headers": dict(headers or {})}
        if json_body is not None:
            call["json"] = dict(json_body)
        self.calls.append((url, call))
        if not self.responses:
            raise AssertionError(f"нет подготовленного ответа для {url}")
        response = self.responses.pop(0)
        if isinstance(response, HttpError):
            raise response
        return response


class FakeRateLimiter:
    """Лимитер без задержек: фиксирует вызовы и штрафы."""

    def __init__(self) -> None:
        self.acquired: list[SourceKey] = []
        self.penalties: list[tuple[SourceKey, float | None]] = []

    async def acquire(self, source_key: SourceKey) -> None:
        self.acquired.append(source_key)

    async def penalize(self, source_key: SourceKey, retry_after: float | None) -> None:
        self.penalties.append((source_key, retry_after))


class ScriptedAdapter:
    """Адаптер источника с заранее заданной выдачей (для проверок RunCollection)."""

    def __init__(
        self,
        key: SourceKey,
        documents: Sequence[RawDocument],
        http_requests: int = 1,
        failure: Exception | None = None,
        clock: FakeClock | None = None,
        advance_seconds: float = 0.0,
    ) -> None:
        self.key = key
        self.raw_meta_keys = frozenset({"topics", "categories", "concepts", "type", "feed", "language"})
        self._documents = list(documents)
        self._http_requests = http_requests
        self._failure = failure
        self._clock = clock
        self._advance_seconds = advance_seconds

    @property
    def http_requests(self) -> int:
        return self._http_requests

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        accounting = current_request_accounting()  # как у настоящего адаптера: запросы в учёт сбора
        if accounting is not None:
            for _ in range(self._http_requests):
                accounting.count_request(self.key)
        for document in self._documents:
            if self._clock is not None and self._advance_seconds:
                self._clock.advance(self._advance_seconds)  # имитация расхода бюджета времени
            yield document
            await asyncio.sleep(0)
        if self._failure is not None:
            raise self._failure


def raw_document(url: str, title: str = "Документ", **kwargs: Any) -> RawDocument:
    """Сырой документ с разумными значениями по умолчанию."""
    return RawDocument(
        url=url,
        title=title,
        text=kwargs.pop("text", "Краткое описание технологии для теста."),
        matched_term=kwargs.pop("matched_term", "тест"),
        published_at=kwargs.pop("published_at", BASE_TIME),
        **kwargs,
    )


def status_error(status: int, retry_after: float | None = None) -> HttpStatusError:
    """Ошибка HTTP-статуса для кассет."""
    return HttpStatusError(status, f"код {status}", retry_after)
