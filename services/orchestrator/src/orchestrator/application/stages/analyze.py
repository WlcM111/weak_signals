"""Стадия 3 — анализ коллекции сервисом analyzer с опросом до терминального статуса (§7.3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orchestrator.application.dto import AnalysisView
from orchestrator.application.ports import AnalyzerClient
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import JobErrorCode
from ws_common.clock import Clock
from ws_common.logging import get_logger

log = get_logger("orchestrator.stage.analyze")


@dataclass(frozen=True, slots=True)
class AnalyzeConfig:
    """Параметры стадии анализа."""

    timeout_seconds: int = 300
    poll_seconds: float = 3.0


async def run_analyze(
    analyzer: AnalyzerClient,
    job_id: str,
    collection_id: str,
    query_text: str,
    top_n: int,
    config: AnalyzeConfig,
    clock: Clock,
    check: Callable[[], Awaitable[None]],
    existing_analysis_id: str = "",
    on_started: Callable[[str], Awaitable[None]] | None = None,
) -> AnalysisView:
    """Запускает или переиспользует анализ и дожидается его завершения."""
    try:
        analysis_id = existing_analysis_id or await analyzer.start_analysis(
            idempotency_key=f"{job_id}:analyze",
            collection_id=collection_id,
            query_text=query_text,
            top_n=top_n,
        )
    except UpstreamUnavailable as error:
        raise StageFailed(
            JobErrorCode.UPSTREAM_UNAVAILABLE.value, f"analyzer недоступен: {error.message}", True
        ) from error
    if on_started is not None:
        await on_started(analysis_id)  # ссылка сохраняется до опроса — см. комментарий в collect
    deadline = clock.monotonic() + config.timeout_seconds
    while True:
        await check()
        try:
            view = await analyzer.get_analysis(analysis_id)
        except UpstreamUnavailable as error:
            raise StageFailed(
                JobErrorCode.UPSTREAM_UNAVAILABLE.value, f"analyzer недоступен: {error.message}", True
            ) from error
        if view.is_terminal:
            break
        if clock.monotonic() >= deadline:
            try:
                await analyzer.cancel_analysis(analysis_id, "stage timeout")  # не оставлять фоновую работу
            except Exception as error:  # noqa: BLE001 - отмена не должна маскировать таймаут
                log.warning("stage.analyze.cancel_failed", analysis_id=analysis_id, error=str(error)[:200])
            raise StageFailed(
                JobErrorCode.ANALYSIS_FAILED.value,
                f"анализ не завершился за {config.timeout_seconds} с",
            )
        await clock.sleep(config.poll_seconds)
    log.info(
        "stage.analyze",
        analysis_id=analysis_id,
        status=view.status,
        weak_signals=view.weak_signals_total,
    )
    if view.status in {"FAILED", "CANCELLED"}:
        raise StageFailed(
            JobErrorCode.ANALYSIS_FAILED.value,
            f"анализ завершился статусом {view.status}: {view.error_code or 'без кода'}",
        )
    return view
