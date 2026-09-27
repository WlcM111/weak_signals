"""Composition root orchestrator: `python -m orchestrator.main api|worker` (§9 HANDOFF)."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from orchestrator.adapters.inbound.http.deps import ApiKeyGuard, RateLimiter
from orchestrator.adapters.inbound.http.handlers import ApiHandlers
from orchestrator.adapters.outbound.metrics import PrometheusMetrics
from orchestrator.adapters.outbound.postgres.idempotency_repository import (
    PostgresIdempotencyRepository,
)
from orchestrator.adapters.outbound.postgres.job_repository import PostgresJobRepository
from orchestrator.adapters.outbound.postgres.result_repository import PostgresResultRepository
from orchestrator.application.stages.analyze import AnalyzeConfig
from orchestrator.application.stages.collect import CollectConfig
from orchestrator.application.stages.expand import Glossary
from orchestrator.application.stages.narrate import NarrateConfig
from orchestrator.application.use_cases.cancel_job import CancelJob
from orchestrator.application.use_cases.get_job import GetJob, ListJobs
from orchestrator.application.use_cases.get_results import GetResultItem, GetResults
from orchestrator.application.use_cases.proxy import GetModelInfo, ScoreText
from orchestrator.application.use_cases.run_job import RunJob, RunJobConfig
from orchestrator.application.use_cases.submit_query import SubmitQuery
from orchestrator.application.use_cases.worker import Cleanup, ReleaseExpiredLeases, WorkerLoop
from orchestrator.config import OrchestratorSettings
from ws_common.clock import SystemClock
from ws_common.config import load_settings
from ws_common.db import build_pool
from ws_common.ids import worker_id
from ws_common.logging import configure_logging, get_logger
from ws_common.metrics import render_metrics
from ws_common.migrate import apply_migrations

SCHEMA = "orchestrator"
log = get_logger("orchestrator.main")


async def _build_context(settings: OrchestratorSettings) -> dict[str, Any]:
    """Создаёт пул БД, gRPC-каналы, репозитории и клиентов — общую часть api и worker."""
    import grpc  # noqa: PLC0415 - тяжёлая зависимость нужна только в рабочем контуре

    from orchestrator.adapters.outbound.grpc.clients import (  # noqa: PLC0415
        GrpcAnalyzerClient,
        GrpcCollectorClient,
        GrpcInsightClient,
    )

    pool = build_pool(
        settings.pg_dsn,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        statement_timeout_ms=settings.pg_statement_timeout_ms,
        application_name=settings.service_name,
    )
    await pool.open(wait=True, timeout=30)
    applied = await apply_migrations(pool, SCHEMA, settings.migrations_dir)
    log.info("migrations.applied", versions=applied)
    channels = {
        "collector": grpc.aio.insecure_channel(settings.collector_addr),
        "analyzer": grpc.aio.insecure_channel(settings.analyzer_addr),
    }
    insight_client = None
    if settings.insight_enabled:
        channels["insight"] = grpc.aio.insecure_channel(settings.insight_addr)
        insight_client = GrpcInsightClient(channels["insight"])
    return {
        "pool": pool,
        "channels": channels,
        "jobs": PostgresJobRepository(pool),
        "results": PostgresResultRepository(pool),
        "idempotency": PostgresIdempotencyRepository(pool),
        "collector": GrpcCollectorClient(channels["collector"]),
        "analyzer": GrpcAnalyzerClient(channels["analyzer"]),
        "insight": insight_client,
        "metrics": PrometheusMetrics(),
        "clock": SystemClock(),
    }


async def _shutdown(context: dict[str, Any]) -> None:
    """Закрывает каналы и пул соединений."""
    for channel in context["channels"].values():
        await channel.close()
    await context["pool"].close()


def build_handlers(context: dict[str, Any], settings: OrchestratorSettings) -> ApiHandlers:
    """Собирает обработчики HTTP из сценариев."""
    jobs = context["jobs"]
    results = context["results"]

    async def readiness() -> dict[str, bool]:
        """Готовность: доступность БД."""
        try:
            await jobs.count_pending()
        except Exception:  # noqa: BLE001 - любая ошибка означает неготовность
            return {"database": False}
        return {"database": True}

    return ApiHandlers(
        submit_query=SubmitQuery(
            jobs,
            context["idempotency"],
            context["clock"],
            settings.queue_max_pending,
            settings.idempotency_ttl_hours,
        ),
        get_job=GetJob(jobs, results),
        list_jobs=ListJobs(jobs, results),
        cancel_job=CancelJob(jobs, results),
        get_results=GetResults(jobs, results),
        get_result_item=GetResultItem(results, jobs),
        get_model_info=GetModelInfo(context["analyzer"]),
        score_text=ScoreText(context["analyzer"]),
        guard=ApiKeyGuard(settings.api_key),
        rate_limiter=RateLimiter(settings.rate_limit_post_per_min),
        ip_salt=settings.ip_hash_salt,
        readiness=readiness,
    )


def build_run_job(context: dict[str, Any], settings: OrchestratorSettings) -> RunJob:
    """Собирает сценарий выполнения задания из клиентов и настроек стадий."""
    return RunJob(
        jobs=context["jobs"],
        results=context["results"],
        collector=context["collector"],
        analyzer=context["analyzer"],
        insight=context["insight"],
        glossary=Glossary.load(settings.glossary_path),
        clock=context["clock"],
        config=RunJobConfig(
            lease_seconds=settings.job_lease_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            deadline_enabled=settings.job_deadline_enabled,
            deadline_seconds=settings.job_deadline_seconds,
            deadline_reserve_seconds=settings.job_deadline_reserve_seconds,
            insight_call_seconds=settings.insight_timeout_seconds,
            collect=CollectConfig(
                time_budget_seconds=settings.collect_time_budget_seconds,
                max_total_documents=settings.collect_max_total_documents,
                min_documents=settings.min_documents,
                poll_seconds=settings.stage_poll_seconds,
            ),
            analyze=AnalyzeConfig(
                timeout_seconds=settings.analyze_timeout_seconds,
                poll_seconds=settings.stage_poll_seconds,
            ),
            narrate=NarrateConfig(
                evidence_text_max_chars=settings.evidence_text_max_chars,
                prompt_version=settings.prompt_version,
                judge_enabled=settings.candidate_judge_enabled,
                judge_pool=settings.candidate_judge_pool,
                judge_order=settings.candidate_judge_order,
                selection_mode=settings.selection_mode,
                rubric_pool=settings.rubric_pool,
                finalize_enabled=settings.finalize_enabled,
                rubric_legacy_fallback=settings.rubric_legacy_fallback,
                rubric_fill_uncertain=settings.rubric_fill_uncertain,
                stage_calibration=settings.stage_calibration,
                trend_calibration=settings.trend_calibration,
                rubric_model_path=settings.rubric_model_path,
                rubric_min_probability=settings.rubric_min_probability,
                rubric_min_cards=settings.rubric_min_cards,
                signal_model_path=settings.signal_model_path,
            ),
        ),
        metrics=context["metrics"],
    )


async def run_api(settings: OrchestratorSettings) -> int:
    """Поднимает HTTP API на uvicorn."""
    import uvicorn  # noqa: PLC0415 - веб-сервер нужен только процессу api

    from orchestrator.adapters.inbound.http.app import create_app  # noqa: PLC0415

    context = await _build_context(settings)
    app = create_app(build_handlers(context, settings))
    config = uvicorn.Config(
        app,
        host=settings.api_bind,
        port=settings.http_port,
        log_config=None,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",  # API стоит за nginx контейнера ui: адрес клиента приходит в X-Forwarded-For
    )
    server = uvicorn.Server(config)
    log.info("service.started", role="api", bind=settings.api_bind, port=settings.http_port)
    try:
        await server.serve()
    finally:
        await _shutdown(context)
    return 0


async def serve_worker_ops(host: str, port: int, stop: asyncio.Event) -> None:
    """Служебный HTTP воркера: `/healthz` (healthcheck compose) и `/metrics` (Prometheus) до сигнала `stop`."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await asyncio.wait_for(reader.readline(), timeout=5)).split()
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (b"\r\n", b"\n", b""):
                pass
            path = parts[1].decode("ascii", "replace") if len(parts) > 1 else ""
            if path == "/healthz":
                status, body, ctype = "200 OK", b'{"status":"ok"}', "application/json"
            elif path == "/metrics":
                status, body, ctype = "200 OK", render_metrics(), "text/plain; version=0.0.4"
            else:
                status, body, ctype = "404 Not Found", b"", "text/plain"
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            )
            writer.write(head.encode("ascii") + body)
            await writer.drain()
        except (TimeoutError, ConnectionError) as exc:
            log.debug("ops.request_failed", error=type(exc).__name__)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host=host, port=port)
    async with server:
        await stop.wait()


async def run_worker(settings: OrchestratorSettings) -> int:
    """Поднимает цикл выполнения заданий и фоновые задачи обслуживания."""
    context = await _build_context(settings)
    loop = WorkerLoop(
        jobs=context["jobs"],
        run_job=build_run_job(context, settings),
        clock=context["clock"],
        worker_id=worker_id(settings.service_name),
        lease_seconds=settings.job_lease_seconds,
        poll_seconds=settings.worker_poll_seconds,
        concurrency=settings.worker_concurrency,
        metrics=context["metrics"],
    )
    sweeper = ReleaseExpiredLeases(context["jobs"], context["clock"])
    cleanup = Cleanup(
        context["idempotency"],
        context["jobs"],
        context["results"],
        context["clock"],
        settings.retention_interval_minutes,
        context["metrics"],
    )
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    log.info("service.started", role="worker", concurrency=settings.worker_concurrency)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(loop.run_forever(stop))
            group.create_task(sweeper.run_forever(stop))
            group.create_task(cleanup.run_forever(stop))
            group.create_task(serve_worker_ops(settings.api_bind, settings.http_port, stop))
    finally:
        await _shutdown(context)
    log.info("service.stopped", role="worker")
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT → корректная остановка."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


def main(argv: list[str]) -> int:
    """Точка входа CLI: `api` или `worker`."""
    if len(argv) < 2 or argv[1] not in {"api", "worker"}:
        print("использование: python -m orchestrator.main api|worker", file=sys.stderr)
        return 2
    settings = load_settings(OrchestratorSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    runner = run_api if argv[1] == "api" else run_worker
    return asyncio.run(runner(settings))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
