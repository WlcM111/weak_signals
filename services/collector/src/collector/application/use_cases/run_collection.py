"""Сценарий RunCollection: выполнение сбора воркером (§7 HANDOFF, §9.2 ТЗ).

Схема: по задаче на адаптер в `asyncio.TaskGroup` с общим дедлайном `time_budget_seconds`;
единственный потребитель забирает документы round-robin (интерливинг рангов), дедуплицирует,
пишет партиями ≤ 50 документов в одной транзакции и продлевает аренду heartbeat-ом.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any

from collector.application.dto import AdapterDelta, BatchItem
from collector.application.request_accounting import RequestAccounting, begin_request_accounting
from collector.application.trace_recording import CollectionTrace, DocumentEvent, begin_collection_trace
from collector.application.ports import (
    AdapterRunRepository,
    CollectionRepository,
    CollectionTraceRepository,
    DocumentRepository,
    MetricsSink,
    NullMetrics,
    SourceAdapter,
)
from collector.domain.classify import ClassificationConfig
from collector.domain.dedup import DedupIndex
from collector.domain.entities import AdapterRun, Collection, RawDocument, compute_final_status
from collector.domain.errors import AdapterFailure, InvariantViolation, LeaseLost
from collector.domain.normalization import build_document_draft
from collector.domain.technology_mining import mine_technologies, targeted_since
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, OperationStatus, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

_SENTINEL = object()


PHASE_BROAD = "broad"
PHASE_TARGETED = "targeted"


class StopReason(StrEnum):
    """Причина остановки потребителя."""

    DRAINED = auto()
    CAP_REACHED = auto()
    BUDGET_EXHAUSTED = auto()
    CANCELLED = auto()


@dataclass(frozen=True, slots=True)
class RunCollectionConfig:
    """Параметры выполнения сбора."""

    lease_seconds: int = 60
    heartbeat_seconds: int = 20
    batch_size: int = 50
    queue_maxsize: int = 100
    idle_poll_seconds: float = 0.2
    # Этап 4: после широкого прохода по фразам темы — прицельный проход по технологиям (от модели и извлечённым
    # из заголовков широкого прохода): по `targeted_per_term` документов на технологию в каждом источнике,
    # по релевантности в окне `targeted_window_months`. Широкий проход получает `broad_time_share` бюджета
    # времени и уступает прицельному `targeted_reserve_documents` мест общего лимита; лимит широкого прохода
    # на источник — `broad_max_documents_per_source` (RSS не ограничивается: новости не вытесняются).
    targeted_enabled: bool = True
    targeted_per_term: int = 3
    broad_time_share: float = 0.5
    targeted_reserve_documents: int = 300
    broad_max_documents_per_source: int = 60
    mined_technologies: int = 15
    max_technologies: int = 40
    targeted_window_months: int = 24


class RunCollection:
    """Выполняет сбор документов по коллекции, захваченной воркером."""

    def __init__(
        self,
        collections: CollectionRepository,
        documents: DocumentRepository,
        adapter_runs: AdapterRunRepository,
        adapters: Mapping[SourceKey, SourceAdapter],
        classification: ClassificationConfig,
        clock: Clock,
        config: RunCollectionConfig,
        unavailable_sources: Mapping[SourceKey, AdapterErrorCode] | None = None,
        metrics: MetricsSink | None = None,
        traces: CollectionTraceRepository | None = None,
    ) -> None:
        self._collections = collections
        self._documents = documents
        self._adapter_runs = adapter_runs
        self._adapters = dict(adapters)
        self._classification = classification
        self._clock = clock
        self._config = config
        self._unavailable = dict(unavailable_sources or {})
        self._metrics: MetricsSink = metrics or NullMetrics()
        # Трасса сбора (аудит A3, режим А): None — выключена (WS_COLLECTOR_TRACE_ENABLED=false).
        self._traces = traces
        self._log = get_logger("collector.run_collection")

    async def execute(self, collection: Collection, owner: str) -> OperationStatus:
        """Проводит сбор до терминального статуса и возвращает его."""
        started = self._clock.monotonic()
        runs = {run.source_key: run for run in await self._adapter_runs.list_runs(collection.collection_id)}
        if not runs:  # защита от рассогласования: список источников хранится в adapter_runs
            runs = {key: AdapterRun(source_key=key) for key in collection.sources}
        state = _RunState(
            collection=collection,
            runs=runs,
            deadline=started + collection.limits.time_budget_seconds,
            documents_total=collection.documents_total,
        )
        # Учёт запросов этого сбора: адаптеры общие на процесс, их собственные счётчики накопительные.
        state.accounting = begin_request_accounting()
        # Трасса открывается в том же контексте: задачи адаптеров наследуют её вместе с учётом запросов.
        state.trace = begin_collection_trace() if self._traces is not None else None
        self._log.info(
            "collection.started",
            collection_id=collection.collection_id,
            mode=collection.mode.value,
            terms_count=collection.terms.total,
            sources=[key.value for key in runs],
            time_budget_seconds=collection.limits.time_budget_seconds,
        )
        active = await self._prepare_runs(state)
        try:
            if active:
                await self._run_phases(state, active, owner, started)
            else:
                state.stop_reason = StopReason.DRAINED
        except LeaseLost:
            # аренда принадлежит другому воркеру: ничего не дописываем, коллекцию подхватит он
            self._log.warning("collection.lease_lost", collection_id=collection.collection_id)
            raise
        await self._finalize_runs(state)
        return await self._finish(state, owner, duration=self._clock.monotonic() - started)

    def _targeted(self, collection: Collection, active: Mapping[SourceKey, SourceAdapter]) -> bool:
        """Прицельный проход: включён, режим SEARCH и хотя бы один активный источник его поддерживает."""
        return (self._config.targeted_enabled and collection.mode is CollectionMode.SEARCH
                and any(getattr(adapter, "supports_targeted", False) for adapter in active.values()))

    def _broad_limits(self, state: _RunState, source_key: SourceKey) -> CollectionLimits:
        """Лимиты широкого прохода: при прицельном проходе источник (кроме RSS) получает меньшую долю."""
        limits = state.collection.limits
        if not state.two_phase or source_key is SourceKey.RSS:
            return limits
        cap = min(limits.max_documents_per_source, self._config.broad_max_documents_per_source)
        return dataclasses.replace(limits, max_documents_per_source=cap)

    async def _run_phases(
        self, state: _RunState, active: dict[SourceKey, SourceAdapter], owner: str, started: float
    ) -> None:
        """Широкий проход по фразам темы, затем — прицельный по технологиям (этап 4)."""
        limits = state.collection.limits
        targeted = self._targeted(state.collection, active)
        final_deadline = started + limits.time_budget_seconds
        if targeted:
            state.two_phase = True
            state.deadline = started + limits.time_budget_seconds * self._config.broad_time_share
            reserve = min(self._config.targeted_reserve_documents, limits.max_total_documents // 2)
            state.total_cap = max(1, limits.max_total_documents - reserve)
        await self._collect(
            state, active, owner,
            lambda key, adapter: adapter.search(state.collection.terms, self._broad_limits(state, key), state.deadline),
        )
        if not targeted or state.stop_reason is StopReason.CANCELLED or state.lease_lost:
            return
        technologies = await self._technologies(state)
        sources = {
            key: adapter for key, adapter in active.items()
            if getattr(adapter, "supports_targeted", False) and not state.runs[key].status.is_terminal
        }
        if not technologies or not sources or self._clock.monotonic() >= final_deadline:
            return
        state.phase = PHASE_TARGETED
        state.deadline = final_deadline
        state.total_cap = limits.max_total_documents
        state.stop_reason = None
        since = targeted_since(self._clock.now(), self._config.targeted_window_months, limits.published_since_year)
        self._log.info("collection.targeted", collection_id=state.collection.collection_id,
                       technologies=len(technologies), sources=[key.value for key in sources], since=since.isoformat())
        await self._collect(
            state, sources, owner,
            lambda _key, adapter: adapter.search_targeted(technologies, self._config.targeted_per_term, since, state.deadline),
        )

    async def _technologies(self, state: _RunState) -> tuple[str, ...]:
        """Технологии прицельного прохода: сначала от модели, затем извлечённые из заголовков широкого прохода."""
        terms = state.collection.terms
        mined = mine_technologies(state.titles, exclude=(*terms.en, *terms.ru, *terms.technologies),
                                  limit=self._config.mined_technologies)
        try:
            await self._collections.save_mined_technologies(state.collection.collection_id, mined)
        except Exception as exc:  # noqa: BLE001 — список извлечённых технологий не должен срывать сбор
            self._log.warning("collection.mined_save_failed", collection_id=state.collection.collection_id, error=str(exc))
        merged: dict[str, str] = {}
        for item in (*terms.technologies, *mined):
            merged.setdefault(" ".join(item.lower().split()), item)
        technologies = tuple(merged.values())[: self._config.max_technologies]
        self._log.info("collection.technologies", collection_id=state.collection.collection_id,
                       from_model=len(terms.technologies), mined=mined, total=len(technologies))
        return technologies

    async def _prepare_runs(self, state: _RunState) -> dict[SourceKey, SourceAdapter]:
        """Отмечает недоступные адаптеры и переводит остальные в RUNNING."""
        now = self._clock.now()
        active: dict[SourceKey, SourceAdapter] = {}
        for source_key, run in state.runs.items():
            adapter = self._adapters.get(source_key)
            unavailable_code = self._unavailable.get(source_key)
            if adapter is None or unavailable_code is not None:
                code = unavailable_code or AdapterErrorCode.DISABLED
                run.fail(now, code, _UNAVAILABLE_MESSAGES[code])
                await self._adapter_runs.save(state.collection.collection_id, run)
                continue
            run.start(now)
            active[source_key] = adapter
        return active

    async def _collect(
        self,
        state: _RunState,
        active: dict[SourceKey, SourceAdapter],
        owner: str,
        iterate: Callable[[SourceKey, SourceAdapter], AsyncIterator[RawDocument]],
    ) -> None:
        """Запускает задачи адаптеров, heartbeat и потребителя партий."""
        queues = {key: asyncio.Queue[Any](maxsize=self._config.queue_maxsize) for key in active}
        wakeup = asyncio.Event()
        failure: Exception | None = None
        async with asyncio.TaskGroup() as group:
            producers = {
                key: group.create_task(self._produce(state, key, iterate(key, adapter), queues[key], wakeup))
                for key, adapter in active.items()
            }
            heartbeat = group.create_task(self._heartbeat(state, owner, wakeup))
            try:
                await self._consume(state, active, queues, wakeup)
            except Exception as exc:  # noqa: BLE001 - пробрасывается после остановки задач группы
                failure = exc  # TaskGroup обернул бы исключение тела в ExceptionGroup
            finally:
                for task in producers.values():
                    task.cancel()
                heartbeat.cancel()
        if failure is not None:
            raise failure

    async def _produce(
        self,
        state: _RunState,
        source_key: SourceKey,
        documents: AsyncIterator[RawDocument],
        queue: asyncio.Queue[Any],
        wakeup: asyncio.Event,
    ) -> None:
        """Задача адаптера: перекладывает документы в очередь; исключения наружу не выпускает."""
        run = state.runs[source_key]
        cancelled = False
        try:
            async for raw in documents:
                await queue.put(raw)
                wakeup.set()
        except AdapterFailure as failure:
            run.fail(self._clock.now(), AdapterErrorCode(failure.code), failure.message)
        except asyncio.CancelledError:
            cancelled = True
            state.cancelled_producers.add(source_key)  # кооперативная остановка: бюджет, лимит или отмена
        except Exception as exc:  # noqa: BLE001 - адаптер обязан не ронять сбор целиком
            run.fail(self._clock.now(), AdapterErrorCode.PARSE_ERROR, f"непредвиденная ошибка адаптера: {exc}")
            self._log.error("adapter.crashed", source=source_key.value, error=str(exc))
        finally:
            if cancelled:
                # потребитель уже остановлен: ожидание места в полной очереди повисло бы навсегда
                # и заблокировало выход из TaskGroup
                if not queue.full():
                    queue.put_nowait(_SENTINEL)
            else:
                await queue.put(_SENTINEL)
            wakeup.set()

    async def _heartbeat(self, state: _RunState, owner: str, wakeup: asyncio.Event) -> None:
        """Продлевает аренду и отслеживает запрос отмены."""
        try:
            while True:
                await self._clock.sleep(self._config.heartbeat_seconds)
                lease = await self._collections.heartbeat(
                    state.collection.collection_id, owner, self._config.lease_seconds
                )
                if not lease.alive:
                    state.lease_lost = True
                    wakeup.set()
                    return
                if lease.cancel_requested:
                    state.cancel_requested = True
                    wakeup.set()
                    return
        except asyncio.CancelledError:
            return

    async def _consume(
        self,
        state: _RunState,
        active: dict[SourceKey, SourceAdapter],
        queues: dict[SourceKey, asyncio.Queue[Any]],
        wakeup: asyncio.Event,
    ) -> None:
        """Round-robin по адаптерам: по одному документу за проход, запись партиями."""
        batch: list[BatchItem] = []
        finished: set[SourceKey] = set()
        order = list(active)
        while True:
            progressed = False
            for source_key in order:
                if source_key in finished:
                    continue
                queue = queues[source_key]
                if queue.empty():
                    continue
                item = queue.get_nowait()
                if item is _SENTINEL:
                    finished.add(source_key)
                    continue
                progressed = True
                self._append_document(state, source_key, item, batch)
                if len(batch) >= self._config.batch_size:
                    await self._flush(state, active, batch)
            stop = self._stop_reason(state, finished, order)
            if stop is not None:
                state.stop_reason = stop
                break
            if not progressed:
                wakeup.clear()
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=self._config.idle_poll_seconds)
                except TimeoutError:
                    pass
        if state.lease_lost:
            raise LeaseLost("аренда потеряна во время сбора")
        await self._flush(state, active, batch)

    def _stop_reason(
        self, state: _RunState, finished: set[SourceKey], order: list[SourceKey]
    ) -> StopReason | None:
        """Определяет, нужно ли прекратить потребление."""
        if state.lease_lost:
            return StopReason.CANCELLED
        if state.cancel_requested:
            return StopReason.CANCELLED
        if state.documents_total + len(state.pending_new) >= state.total_cap:
            return StopReason.CAP_REACHED
        if self._clock.monotonic() >= state.deadline:
            return StopReason.BUDGET_EXHAUSTED
        if len(finished) == len(order):
            return StopReason.DRAINED
        return None

    def _append_document(
        self, state: _RunState, source_key: SourceKey, raw: RawDocument, batch: list[BatchItem]
    ) -> None:
        """Нормализует документ, применяет дедупликацию и лимит на источник."""
        run = state.runs[source_key]
        run.documents_found += 1
        state.found_delta[source_key] = state.found_delta.get(source_key, 0) + 1
        if state.phase == PHASE_BROAD:
            cap = self._broad_limits(state, source_key).max_documents_per_source
            if state.per_source_accepted.get(source_key, 0) >= cap:
                _note(state, source_key, raw, DocumentEvent.SOURCE_CAP, detail=f"лимит {cap} документов на источник")
                return
        # прицельный проход ограничен квотой на технологию в самом адаптере и общим лимитом сбора
        adapter = self._adapters[source_key]
        try:
            draft = build_document_draft(
                raw,
                source_key=source_key,
                config=self._classification,
                raw_meta_keys=adapter.raw_meta_keys,
                fetched_at=self._clock.now(),
            )
        except (ValueError, InvariantViolation) as exc:
            self._log.debug("document.rejected", source=source_key.value, reason=str(exc))
            _note(state, source_key, raw, DocumentEvent.REJECTED, detail=str(exc))
            return
        duplicate_of = state.dedup.register_if_new(draft.url_hash, draft.content_hash, draft.doi)
        if duplicate_of is not None:
            self._log.debug("dedup.hit", source=source_key.value, url_hash=draft.url_hash[:12])
            _note(state, source_key, raw, DocumentEvent.DEDUP, url_hash=draft.url_hash, doi=draft.doi,
                  detail=f"дубль: {duplicate_of}")
            return
        state.rank += 1
        state.per_source_accepted[source_key] = state.per_source_accepted.get(source_key, 0) + 1
        if state.phase == PHASE_BROAD:
            state.titles.append((draft.title, source_key.value))
        state.pending_new.add(draft.url_hash)
        batch.append(BatchItem(draft=draft, relevance_rank=state.rank))
        _note(state, source_key, raw, DocumentEvent.ACCEPTED, url_hash=draft.url_hash, doi=draft.doi,
              detail=f"relevance_rank={state.rank}")

    async def _flush(
        self, state: _RunState, active: Mapping[SourceKey, SourceAdapter], batch: list[BatchItem]
    ) -> None:
        """Записывает партию документов и прирост счётчиков в одной транзакции."""
        deltas: dict[SourceKey, AdapterDelta] = {}
        for source_key in active:
            http_delta = max(0, state.accounting.requests_of(source_key) - state.http_seen.get(source_key, 0))
            found_delta = state.found_delta.get(source_key, 0)
            if http_delta or found_delta:
                deltas[source_key] = AdapterDelta(http_requests=http_delta, documents_found=found_delta)
        if not batch and not deltas:
            return
        outcome = await self._documents.persist_batch(
            state.collection.collection_id, tuple(batch), deltas
        )
        for source_key, delta in deltas.items():
            state.http_seen[source_key] = state.http_seen.get(source_key, 0) + delta.http_requests
            state.runs[source_key].http_requests += delta.http_requests
        for source_key, count in outcome.new_by_source.items():
            state.runs[source_key].documents_new += count
            self._metrics.documents_new(source_key, count)
        state.found_delta.clear()
        state.pending_new.clear()
        state.documents_total = outcome.documents_total
        batch.clear()
        await self._save_trace(state)

    async def _finalize_runs(self, state: _RunState) -> None:
        """Проставляет терминальные статусы запусков адаптеров и сохраняет их."""
        now = self._clock.now()
        for source_key, run in state.runs.items():
            if not run.status.is_terminal:
                if state.cancel_requested or state.lease_lost:
                    run.cancel(now)
                elif state.stop_reason is StopReason.BUDGET_EXHAUSTED:
                    run.complete(now, AdapterErrorCode.BUDGET_EXHAUSTED)
                else:
                    run.complete(now)
            # сохраняются все запуски, включая отказавшие внутри задачи адаптера: их статус
            # выставлен в памяти и без этой записи остался бы в БД как PENDING
            await self._adapter_runs.save(state.collection.collection_id, run)
            self._metrics.adapter_finished(source_key, run.status)
            self._log.info(
                "adapter.run",
                collection_id=state.collection.collection_id,
                source=source_key.value,
                status=run.status.value,
                found=run.documents_found,
                new=run.documents_new,
                http_requests=run.http_requests,
                error_code=run.error_code,
            )

    async def _finish(self, state: _RunState, owner: str, duration: float) -> OperationStatus:
        """Вычисляет итоговый статус коллекции и фиксирует его."""
        await self._save_trace(state)
        status, error_code, error_message = compute_final_status(
            list(state.runs.values()),
            state.documents_total,
            cancelled=state.cancel_requested,
            budget_exhausted=state.stop_reason is StopReason.BUDGET_EXHAUSTED,
        )
        await self._collections.finish(
            state.collection.collection_id,
            owner,
            status,
            error_code=error_code,
            error_message=error_message,
            finished_at=self._clock.now(),
        )
        self._metrics.collection_finished(state.collection.mode.value, status, duration)
        self._log.info(
            "collection.finished",
            collection_id=state.collection.collection_id,
            mode=state.collection.mode.value,
            status=status.value,
            documents_total=state.documents_total,
            http_requests_total=sum(run.http_requests for run in state.runs.values()),
            duration_ms=round(duration * 1000, 2),
            error_code=error_code,
            trace_save_failures=state.trace.save_failures if state.trace is not None else 0,
        )
        return status

    async def _save_trace(self, state: _RunState) -> None:
        """Сохраняет накопленную трассу; сбой записи не обрывает сбор, но виден в журнале и счётчике."""
        if self._traces is None or state.trace is None:
            return
        responses, events = state.trace.drain()
        if not responses and not events:
            return
        try:
            await self._traces.append(state.collection.collection_id, state.trace.run_id, responses, events)
        except Exception as error:  # noqa: BLE001 - потеря трассы не должна стоить пользователю выдачи
            state.trace.save_failures += 1
            self._log.error(
                "collection.trace_failed",
                collection_id=state.collection.collection_id,
                responses=len(responses),
                events=len(events),
                error=str(error)[:300],
            )


_UNAVAILABLE_MESSAGES = {
    AdapterErrorCode.DISABLED: "адаптер отключён в каталоге источников",
    AdapterErrorCode.AUTH_MISSING: "не задан ключ доступа к источнику",
}


@dataclass(slots=True)
class _RunState:
    """Изменяемое состояние одного выполнения сбора."""

    collection: Collection
    runs: dict[SourceKey, AdapterRun]
    deadline: float
    documents_total: int
    rank: int = 0
    stop_reason: StopReason | None = None
    cancel_requested: bool = False
    lease_lost: bool = False
    dedup: DedupIndex = field(default_factory=DedupIndex)
    per_source_accepted: dict[SourceKey, int] = field(default_factory=dict)
    http_seen: dict[SourceKey, int] = field(default_factory=dict)
    found_delta: dict[SourceKey, int] = field(default_factory=dict)
    pending_new: set[str] = field(default_factory=set)
    cancelled_producers: set[SourceKey] = field(default_factory=set)
    accounting: RequestAccounting = field(default_factory=RequestAccounting)
    trace: CollectionTrace | None = None
    # Этап 4: проход сбора, общий лимит текущего прохода, заголовки широкого прохода для извлечения технологий.
    phase: str = "broad"
    two_phase: bool = False
    total_cap: int = 0
    titles: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rank = self.documents_total  # продолжение нумерации при повторном запуске
        self.total_cap = self.total_cap or self.collection.limits.max_total_documents


def _note(
    state: _RunState,
    source_key: SourceKey,
    raw: RawDocument,
    event: DocumentEvent,
    *,
    url_hash: str | None = None,
    doi: str | None = None,
    detail: str | None = None,
) -> None:
    """Событие документа в трассе сбора (если трассировка включена)."""
    if state.trace is None:
        return
    state.trace.add_event(source_key=source_key.value, matched_term=raw.matched_term, event=event, url=raw.url,
                          title=raw.title, url_hash=url_hash, doi=doi or raw.doi, detail=detail)
