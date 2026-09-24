"""Стадия 2 — сбор документов через collector с опросом до терминального статуса (§7.2)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orchestrator.application.dto import CollectionView, ExpansionView
from orchestrator.application.ports import CollectorClient
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import JobErrorCode
from ws_common.clock import Clock
from ws_common.logging import get_logger

POLL_MARGIN_SECONDS = 30
log = get_logger("orchestrator.stage.collect")


@dataclass(frozen=True, slots=True)
class CollectConfig:
    """Параметры стадии сбора."""

    time_budget_seconds: int = 120
    max_total_documents: int = 800
    min_documents: int = 20
    poll_seconds: float = 3.0


async def run_collect(
    collector: CollectorClient,
    job_id: str,
    query_text: str,
    expansion: ExpansionView,
    config: CollectConfig,
    clock: Clock,
    check: Callable[[], Awaitable[None]],
    existing_collection_id: str = "",
    on_started: Callable[[str], Awaitable[None]] | None = None,
) -> CollectionView:
    """Запускает или переиспользует коллекцию и дожидается терминального статуса.

    `existing_collection_id` позволяет повторной попытке задания не собирать документы заново:
    ключ идемпотентности у collector тот же, поэтому повторный вызов вернул бы ту же коллекцию.
    """
    try:
        collection_id = existing_collection_id or await collector.start_collection(
            idempotency_key=f"{job_id}:collect",
            query_text=query_text,
            ru_terms=expansion.ru_terms,
            en_terms=expansion.en_terms,
            time_budget_seconds=config.time_budget_seconds,
            max_total_documents=config.max_total_documents,
        )
    except UpstreamUnavailable as error:
        raise StageFailed(
            JobErrorCode.UPSTREAM_UNAVAILABLE.value, f"collector недоступен: {error.message}", True
        ) from error
    if on_started is not None:
        # идентификатор сохраняется до начала опроса: иначе отмена или падение воркера
        # оставили бы запущенный сбор без ссылки на него
        await on_started(collection_id)
    deadline = clock.monotonic() + config.time_budget_seconds + POLL_MARGIN_SECONDS
    while True:
        await check()
        try:
            view = await collector.get_collection(collection_id)
        except UpstreamUnavailable as error:
            raise StageFailed(
                JobErrorCode.UPSTREAM_UNAVAILABLE.value, f"collector недоступен: {error.message}", True
            ) from error
        if view.is_terminal:
            break
        if clock.monotonic() >= deadline:
            log.warning("stage.collect", collection_id=collection_id, reason="poll_timeout")
            break
        await clock.sleep(config.poll_seconds)
    log.info(
        "stage.collect",
        collection_id=collection_id,
        status=view.status,
        documents=view.documents_total,
        failed_adapters=len(view.failed_adapters),
    )
    if view.status == "FAILED":
        raise StageFailed(JobErrorCode.COLLECTION_FAILED.value, "сбор документов завершился отказом")
    if view.documents_total < config.min_documents:
        raise StageFailed(
            JobErrorCode.TOO_FEW_DOCUMENTS.value,
            f"собрано документов {view.documents_total}, требуется не менее {config.min_documents}",
        )
    return view
