"""Сценарий RunCollection: выполнение сбора воркером (§7 HANDOFF, §9.2 ТЗ).

Схема: по задаче на адаптер в `asyncio.TaskGroup` с общим дедлайном `time_budget_seconds`;
единственный потребитель забирает документы round-robin (интерливинг рангов), дедуплицирует,
пишет партиями ≤ 50 документов в одной транзакции и продлевает аренду heartbeat-ом.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any

from collector.application.dto import AdapterDelta, BatchItem
from collector.application.request_accounting import RequestAccounting, begin_request_accounting
from collector.application.ports import (
    AdapterRunRepository,
    CollectionRepository,
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
from collector.domain.values import AdapterErrorCode, OperationStatus, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

_SENTINEL = object()


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
                await self._collect(state, active, owner)
            else:
                state.stop_reason = StopReason.DRAINED
        except LeaseLost:
            # аренда принадлежит другому воркеру: ничего не дописываем, коллекцию подхватит он
            self._log.warning("collection.lease_lost", collection_id=collection.collection_id)
            raise
        await self._finalize_runs(state)
        return await self._finish(state, owner, duration=self._clock.monotonic() - started)

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

    async def _collect(self, state: _RunState, active: dict[SourceKey, SourceAdapter], owner: str) -> None:
        """Запускает задачи адаптеров, heartbeat и потребителя партий."""
        queues = {key: asyncio.Queue[Any](maxsize=self._config.queue_maxsize) for key in active}
        wakeup = asyncio.Event()
        failure: Exception | None = None
        async with asyncio.TaskGroup() as group:
            producers = {
                key: group.create_task(self._produce(state, key, adapter, queues[key], wakeup))
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
        adapter: SourceAdapter,
        queue: asyncio.Queue[Any],
        wakeup: asyncio.Event,
    ) -> None:
        """Задача адаптера: перекладывает документы в очередь; исключения наружу не выпускает."""
        run = state.runs[source_key]
        cancelled = False
        try:
            async for raw in adapter.search(
                state.collection.terms, state.collection.limits, state.deadline
            ):
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
        if state.documents_total + len(state.pending_new) >= state.collection.limits.max_total_documents:
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
        if state.per_source_accepted.get(source_key, 0) >= state.collection.limits.max_documents_per_source:
            return
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
            return
        if state.dedup.register_if_new(draft.url_hash, draft.content_hash, draft.doi) is not None:
            self._log.debug("dedup.hit", source=source_key.value, url_hash=draft.url_hash[:12])
            return
        state.rank += 1
        state.per_source_accepted[source_key] = state.per_source_accepted.get(source_key, 0) + 1
        state.pending_new.add(draft.url_hash)
        batch.append(BatchItem(draft=draft, relevance_rank=state.rank))

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
        )
        return status


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

    def __post_init__(self) -> None:
        self.rank = self.documents_total  # продолжение нумерации при повторном запуске
