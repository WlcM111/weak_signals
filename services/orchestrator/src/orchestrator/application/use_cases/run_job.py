"""Сценарий RunJob: одна попытка выполнения задания по стадиям (§7 HANDOFF).

Между стадиями и перед каждым обращением к сервисам проверяются отмена и аренда: отмена должна
срабатывать не дольше, чем за один интервал опроса, а потерянная аренда — прекращать работу,
чтобы два воркера не писали один снимок.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from orchestrator.application.dto import AnalysisView, CollectionView, ExpansionView
from orchestrator.application.ports import (
    AnalyzerClient,
    CollectorClient,
    InsightClient,
    JobRepository,
    MetricsSink,
    NullMetrics,
    ResultRepository,
)
from orchestrator.application.stages.analyze import AnalyzeConfig, run_analyze
from orchestrator.application.stages.collect import CollectConfig, run_collect
from orchestrator.application.stages.expand import Glossary, fallback_expansion, run_expand
from orchestrator.application.stages.finalize import build_stats, decide_final_status
from orchestrator.application.stages.narrate import NarrateConfig, NarrateOutcome, run_narrate
from orchestrator.domain.deadline import JobDeadline
from orchestrator.domain.entities import Job
from orchestrator.domain.errors import LeaseLost, StageFailed, UpstreamUnavailable
from orchestrator.domain.values import JobErrorCode, JobStatus
from ws_common.clock import Clock
from ws_common.ids import set_correlation_id
from ws_common.logging import get_logger

POSTPONE_SECONDS = 30
# Время, которое сбор оставляет следующим стадиям: анализ (до 300 с) и нарративы. По прогонам 23.09
# анализ занимает около минуты, нарративы — 1–3,5 минуты; при позднем старте сбор сокращается.
AFTER_COLLECT_SECONDS = 420
# Предел расширения запроса и резерв на нарративы после анализа; повтор после сбоя — только если успевает.
EXPAND_TIMEOUT_SECONDS = 60
NARRATE_MIN_SECONDS = 60
RETRY_MIN_WORK_SECONDS = 180


class CancelRequested(Exception):
    """Внутренний сигнал кооперативной отмены между шагами."""


@dataclass(frozen=True, slots=True)
class RunJobConfig:
    """Настройки выполнения задания."""

    lease_seconds: int = 60
    heartbeat_seconds: int = 20
    # Сквозной срок задания от приёма до сохранённого результата и резерв на завершение.
    # deadline_enabled = False отключает срок полностью (WS_JOB_DEADLINE_ENABLED=false).
    deadline_enabled: bool = True
    deadline_seconds: int = 1200
    deadline_reserve_seconds: int = 120
    # Сколько может занять один вызов insight: LLM не вызывается, если до резерва осталось меньше.
    insight_call_seconds: int = 90
    collect: CollectConfig = CollectConfig()
    analyze: AnalyzeConfig = AnalyzeConfig()
    narrate: NarrateConfig = NarrateConfig()


class RunJob:
    """Выполняет задание, захваченное воркером, до терминального статуса."""

    def __init__(
        self,
        jobs: JobRepository,
        results: ResultRepository,
        collector: CollectorClient,
        analyzer: AnalyzerClient,
        insight: InsightClient | None,
        glossary: Glossary,
        clock: Clock,
        config: RunJobConfig,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._jobs = jobs
        self._results = results
        self._collector = collector
        self._analyzer = analyzer
        self._insight = insight
        self._glossary = glossary
        self._clock = clock
        self._config = config
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("orchestrator.run_job")

    async def execute(self, job: Job, worker_id: str) -> JobStatus:
        """Проводит задание через стадии; возвращает достигнутый статус."""
        control = _JobControl(self._jobs, job, worker_id, self._clock, self._config)
        durations: dict[str, int] = {}
        collection: CollectionView | None = None
        analysis: AnalysisView | None = None
        expansion: ExpansionView | None = None
        outcome = NarrateOutcome()
        self._log.info("job.claimed", job_id=job.job_id, attempt=job.attempt, worker_id=worker_id)
        set_correlation_id(job.job_id)  # все вызовы сервисов по этому заданию ищутся в логах по job_id
        deadline = (
            JobDeadline.for_job(
                job.created_at or self._clock.now(),
                self._config.deadline_seconds,
                self._config.deadline_reserve_seconds,
            )
            if self._config.deadline_enabled
            else None
        )
        if deadline is None:
            self._log.info("job.deadline", job_id=job.job_id, enabled=False)
        else:
            # Срок не отменяет работу: опоздавшее задание собирает с минимальным бюджетом, анализирует
            # и выдаёт карточки из найденных источников без LLM, а не завершается пустым отказом.
            self._log.info(
                "job.deadline",
                job_id=job.job_id,
                enabled=True,
                accepted_at=deadline.accepted_at.isoformat(),
                deadline_at=deadline.deadline_at.isoformat(),
                remaining_seconds=round(deadline.remaining(self._clock.now())),
                expired=deadline.expired(self._clock.now()),
            )
        if deadline is not None and deadline.expired(self._clock.now()):
            # Срок исчерпан ожиданием в очереди: честный отказ вместо работы, которая закончится после 1200 с.
            return await self._fail(
                job, worker_id, JobErrorCode.DEADLINE_EXCEEDED.value,
                "срок задания истёк до начала работы: ожидание в очереди исчерпало бюджет",
            )
        # Аренда продлевается фоновой задачей: один вызов LLM (до 120 с) длиннее аренды (60 с), и без
        # фонового продления задание теряло аренду посреди стадии нарратива и уходило на повтор.
        keepalive = asyncio.create_task(control.keepalive())
        try:
            expansion = await self._timed(
                "expand", durations, _bounded_expand(self._insight, job.query_text, self._glossary, EXPAND_TIMEOUT_SECONDS)
            )
            await control.check()

            await self._jobs.transition(job, JobStatus.COLLECTING, worker_id)

            async def remember_collection(collection_id: str) -> None:
                """Сохраняет ссылку на коллекцию сразу после её создания."""
                job.collection_id = collection_id
                await self._jobs.set_references(job.job_id, collection_id=collection_id)

            collection = await self._timed(
                "collect",
                durations,
                run_collect(
                    self._collector,
                    job.job_id,
                    job.query_text,
                    expansion,
                    replace(
                        self._config.collect,
                        time_budget_seconds=deadline.cap(
                            self._clock.now(),
                            self._config.collect.time_budget_seconds,
                            floor=10,
                            keep_for_later=AFTER_COLLECT_SECONDS,
                        ),
                    )
                    if deadline is not None
                    else self._config.collect,
                    self._clock,
                    control.check,
                    job.collection_id,
                    remember_collection,
                ),
            )
            await control.check()

            await self._jobs.transition(job, JobStatus.ANALYZING, worker_id)

            async def remember_analysis(analysis_id: str) -> None:
                """Сохраняет ссылку на анализ сразу после его создания."""
                job.analysis_id = analysis_id
                await self._jobs.set_references(job.job_id, analysis_id=analysis_id)

            analysis = await self._timed(
                "analyze",
                durations,
                run_analyze(
                    self._analyzer,
                    job.job_id,
                    job.collection_id,
                    analysis_query(job.query_text, expansion),
                    job.requested_top_n,
                    # Анализ ограничен остатком срока с резервом на нарративы; по таймауту анализ отменяется.
                    replace(
                        self._config.analyze,
                        timeout_seconds=deadline.cap(
                            self._clock.now(), self._config.analyze.timeout_seconds, floor=30,
                            keep_for_later=NARRATE_MIN_SECONDS,
                        ),
                    )
                    if deadline is not None
                    else self._config.analyze,
                    self._clock,
                    control.check,
                    job.analysis_id,
                    remember_analysis,
                ),
            )
            await control.check()

            await self._jobs.transition(job, JobStatus.NARRATING, worker_id)
            outcome = await self._timed(
                "narrate",
                durations,
                run_narrate(
                    analyzer=self._analyzer,
                    collector=self._collector,
                    insight=self._insight,
                    results=self._results,
                    job_id=job.job_id,
                    query_text=job.query_text,
                    analysis_id=job.analysis_id,
                    config=_narrate_config(
                        self._config.narrate,
                        job.requested_top_n,
                        (lambda: deadline.allows(self._clock.now(), self._config.insight_call_seconds))
                        if deadline is not None
                        else None,
                    ),
                    check=control.check,
                ),
            )
        except CancelRequested:
            return await self._cancel(job, worker_id)
        except LeaseLost:
            self._log.warning("job.lease_lost", job_id=job.job_id, worker_id=worker_id)
            raise
        except StageFailed as error:
            if (
                error.retryable
                and deadline is not None
                and not deadline.allows(self._clock.now(), POSTPONE_SECONDS + RETRY_MIN_WORK_SECONDS)
            ):
                return await self._fail(
                    job, worker_id, JobErrorCode.DEADLINE_EXCEEDED.value,
                    f"повтор после сбоя не успевает до срока: {error.message}",
                )
            if error.retryable and await self._jobs.postpone(
                job, POSTPONE_SECONDS, worker_id, error.error_code
            ):
                self._log.warning("job.postponed", job_id=job.job_id, reason=error.error_code)
                return JobStatus.QUEUED
            return await self._fail(job, worker_id, error.error_code, error.message)
        except UpstreamUnavailable as error:
            return await self._fail(
                job, worker_id, JobErrorCode.UPSTREAM_UNAVAILABLE.value, error.message
            )
        except Exception as error:  # noqa: BLE001 - неожиданный сбой не должен оставлять аренду
            self._log.error("job.failed", job_id=job.job_id, error=str(error))
            return await self._fail(job, worker_id, JobErrorCode.INTERNAL_ERROR.value, str(error))
        finally:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)

        stats = build_stats(collection, analysis, outcome, expansion, durations)
        await self._results.set_stats(job.job_id, stats)
        completion = decide_final_status(outcome, job.requested_top_n, collection)
        await self._jobs.transition(job, completion.status, worker_id, completion.error_message)
        self._metrics.job_finished(completion.status)
        self._log.info(
            "job.finished",
            job_id=job.job_id,
            status=completion.status.value,
            items=outcome.items_written,
            detail=completion.error_message,
        )
        return completion.status

    async def _timed(self, stage: str, durations: dict[str, int], awaitable):  # noqa: ANN001, ANN202
        """Выполняет стадию с замером длительности."""
        started = time.perf_counter()
        self._log.info("job.stage_started", stage=stage)
        result = await awaitable
        elapsed = time.perf_counter() - started
        durations[stage] = int(elapsed * 1000)
        self._metrics.stage_duration(stage, elapsed)
        self._log.info("job.stage_finished", stage=stage, duration_ms=durations[stage])
        return result

    async def _cancel(self, job: Job, worker_id: str) -> JobStatus:
        """Отменяет выполняющиеся операции сервисов и переводит задание в CANCELLED."""
        if job.status is JobStatus.COLLECTING and job.collection_id:
            await self._safe_cancel(self._collector.cancel_collection(job.collection_id, "job cancelled"))
        if job.status is JobStatus.ANALYZING and job.analysis_id:
            await self._safe_cancel(self._analyzer.cancel_analysis(job.analysis_id, "job cancelled"))
        await self._jobs.transition(job, JobStatus.CANCELLED, worker_id, "CANCELLED_BY_USER")
        self._metrics.job_finished(JobStatus.CANCELLED)
        self._log.info("job.cancelled", job_id=job.job_id)
        return JobStatus.CANCELLED

    async def _safe_cancel(self, awaitable) -> None:  # noqa: ANN001 - корутина отмены
        """Отмена вышестоящей операции не должна мешать завершению задания."""
        try:
            await awaitable
        except Exception as error:  # noqa: BLE001
            self._log.warning("job.cancel_upstream_failed", error=str(error))

    async def _fail(self, job: Job, worker_id: str, code: str, message: str) -> JobStatus:
        """Терминальный отказ задания с кодом и сообщением без секретов."""
        job.error_code = code
        await self._jobs.transition(job, JobStatus.FAILED, worker_id, f"{code}: {message}"[:2000])
        self._metrics.job_finished(JobStatus.FAILED)
        self._log.warning("job.failed", job_id=job.job_id, code=code, message=message[:200])
        return JobStatus.FAILED


def _narrate_config(base: NarrateConfig, top_n: int, llm_allowed: Callable[[], bool] | None) -> NarrateConfig:
    """Конфигурация нарратива с размером выдачи и сроком конкретного задания."""
    return NarrateConfig(
        top_n=top_n,
        evidence_text_max_chars=base.evidence_text_max_chars,
        prompt_version=base.prompt_version,
        llm_allowed=llm_allowed,
        judge_enabled=base.judge_enabled,
        judge_pool=base.judge_pool,
        judge_order=base.judge_order,
        selection_mode=base.selection_mode,
        rubric_pool=base.rubric_pool,
        finalize_enabled=base.finalize_enabled,
        rubric_legacy_fallback=base.rubric_legacy_fallback,
        rubric_fill_uncertain=base.rubric_fill_uncertain,
        stage_calibration=base.stage_calibration,
        trend_calibration=base.trend_calibration,
        rubric_model_path=base.rubric_model_path,
        rubric_min_probability=base.rubric_min_probability,
        rubric_min_cards=base.rubric_min_cards,
        signal_model_path=base.signal_model_path,
        profile_model_path=base.profile_model_path,
        final_check_enabled=base.final_check_enabled,
        confidence_calibration=base.confidence_calibration,
    )


async def _bounded_expand(insight: InsightClient | None, query_text: str, glossary: Glossary, timeout: float):  # noqa: ANN201
    """Расширение запроса с пределом времени; по таймауту — резервные термины (статистика это отражает)."""
    try:
        return await asyncio.wait_for(run_expand(insight, query_text, glossary), timeout)
    except TimeoutError:
        get_logger("orchestrator.run_job").warning("stage.expand", used_fallback=True, reason="timeout")
        return fallback_expansion(query_text, glossary)


class _JobControl:
    """Продление аренды и проверка отмены между шагами стадий."""

    def __init__(
        self, jobs: JobRepository, job: Job, worker_id: str, clock: Clock, config: RunJobConfig
    ) -> None:
        self._jobs = jobs
        self._job = job
        self._worker_id = worker_id
        self._clock = clock
        self._config = config
        self._last_beat = clock.monotonic()
        self._lease_lost = False
        self._cancel_seen = False
        self._log = get_logger("orchestrator.lease")

    async def keepalive(self) -> None:
        """Фоновое продление аренды раз в `heartbeat_seconds`, независимо от длительности шагов стадий."""
        while True:
            await asyncio.sleep(self._config.heartbeat_seconds)
            try:
                alive, cancel_requested = await self._jobs.heartbeat(
                    self._job.job_id, self._worker_id, self._config.lease_seconds
                )
            except Exception as error:  # noqa: BLE001 - сбой БД: аренду продлит следующий такт
                self._log.warning("job.heartbeat_failed", job_id=self._job.job_id, error=str(error))
                continue
            self._last_beat = self._clock.monotonic()
            if not alive:
                self._lease_lost = True
                return
            if cancel_requested:
                self._cancel_seen = True
                return

    async def check(self) -> None:
        """Продлевает аренду не чаще раза в `heartbeat_seconds` и реагирует на отмену."""
        if self._lease_lost:
            raise LeaseLost(f"аренда задания {self._job.job_id} потеряна")
        if self._cancel_seen:
            self._job.cancel_requested = True
            raise CancelRequested
        now = self._clock.monotonic()
        if now - self._last_beat < self._config.heartbeat_seconds:
            return
        self._last_beat = now
        alive, cancel_requested = await self._jobs.heartbeat(
            self._job.job_id, self._worker_id, self._config.lease_seconds
        )
        if not alive:
            raise LeaseLost(f"аренда задания {self._job.job_id} потеряна")
        if cancel_requested:
            self._job.cancel_requested = True
            raise CancelRequested



ANALYSIS_QUERY_SEPARATOR = " | "
ANALYSIS_QUERY_LIMIT = 500


def analysis_query(query_text: str, expansion: ExpansionView | None) -> str:
    """Запрос для analyzer: исходная фраза и её расширения через « | », не длиннее 500 символов.

    Релевантность в analyzer считается по вектору запроса. Английские расширения из ExpandQuery
    нужны, чтобы англоязычные профильные документы не проигрывали русским общим текстам только
    из-за языка. Если расширения нет (задание продолжено после рестарта), уходит исходная фраза.
    """
    if expansion is None:
        return query_text
    result = query_text
    for phrase in (*expansion.en_terms, *expansion.ru_terms):
        phrase = phrase.strip()
        if not phrase or phrase in result:
            continue
        candidate = f"{result}{ANALYSIS_QUERY_SEPARATOR}{phrase}"
        if len(candidate) > ANALYSIS_QUERY_LIMIT:
            break
        result = candidate
    return result