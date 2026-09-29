"""Composition root сервиса collector: `python -m collector.main serve` (§9 HANDOFF)."""

from __future__ import annotations

import asyncio
import ssl

# Таймаут чтения для медленных API (arXiv, GDELT): их ответы на стенде шли 11–16 с при общем лимите 15 с.
SLOW_SOURCE_TIMEOUT_SECONDS = 45.0
import signal
import sys
from collections.abc import Mapping

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from weaksignals.collector.v1 import collector_pb2_grpc

from collector.adapters.inbound.grpc_server import CollectorServicer
from collector.adapters.inbound.http_ops import OpsHttpServer
from collector.adapters.inbound.worker import CollectionWorker, MaintenanceWorker
from collector.adapters.outbound.http_client import HttpxClient
from collector.adapters.outbound.metrics import PrometheusMetrics
from collector.adapters.outbound.postgres.adapter_run_repository import PostgresAdapterRunRepository
from collector.adapters.outbound.postgres.collection_repository import PostgresCollectionRepository
from collector.adapters.outbound.postgres.document_repository import PostgresDocumentRepository
from collector.adapters.outbound.postgres.encyclopedia_cache import PostgresEncyclopediaCache
from collector.adapters.outbound.postgres.source_catalog import CatalogEntry, PostgresSourceCatalog
from collector.adapters.outbound.rate_limiter import TokenBucketRateLimiter
from collector.adapters.outbound.rules_loader import load_classification_config
from collector.adapters.outbound.sources.arxiv import ArxivAdapter
from collector.adapters.outbound.sources.github import GithubAdapter
from collector.adapters.outbound.sources.openalex import OpenAlexAdapter
from collector.adapters.outbound.sources.rss import RssAdapter
from collector.adapters.outbound.sources.semantic_scholar import SemanticScholarAdapter
from collector.adapters.outbound.sources.gdelt import GdeltAdapter
from collector.adapters.outbound.sources.rospatent import RospatentAdapter
from collector.adapters.outbound.sources.zenodo import ZenodoAdapter
from collector.adapters.outbound.sources.wikipedia import WikipediaProbe
from collector.application.ports import SourceAdapter
from collector.application.use_cases.cancel_collection import CancelCollection
from collector.application.use_cases.check_encyclopedia import CheckEncyclopedia
from collector.application.use_cases.get_collection import GetCollection
from collector.application.use_cases.get_documents import GetDocuments
from collector.application.use_cases.purge_documents import PurgeDocuments
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.application.use_cases.start_collection import StartCollection
from collector.application.use_cases.stream_documents import StreamDocuments
from collector.config import CollectorSettings
from collector.domain.values import AdapterErrorCode, SourceKey
from ws_common.clock import SystemClock
from ws_common.config import load_settings
from ws_common.db import build_pool
from ws_common.grpc_interceptors import ObservabilityInterceptor
from ws_common.logging import configure_logging, get_logger
from ws_common.metrics import render_metrics
from ws_common.migrate import apply_migrations

SCHEMA = "collector"
log = get_logger("collector.main")


async def serve(settings: CollectorSettings) -> int:
    """Запускает gRPC-сервер, воркеры коллекций и HTTP-эндпоинты обслуживания."""
    clock = SystemClock()
    metrics = PrometheusMetrics()
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

    catalog = await PostgresSourceCatalog(pool).load()
    classification = load_classification_config(settings.trust_rules_path, settings.rss_domains_path)
    http_client = HttpxClient(
        contact_email=settings.contact_email,
        read_timeout_seconds=settings.collector_http_timeout_seconds,
        max_bytes=settings.http_max_bytes,
        retries=settings.collector_http_retries,
    )
    # arXiv — без внутренних повторов клиента: шлюз частоты и 30-минутная пауза адаптера закрывают все отказы,
    # а повторы 429/5xx в обход шлюза нарушали бы правило arXiv «один запрос в 3 секунды».
    arxiv_http_client = HttpxClient(
        contact_email=settings.contact_email,
        read_timeout_seconds=max(settings.collector_http_timeout_seconds, SLOW_SOURCE_TIMEOUT_SECONDS),
        max_bytes=settings.http_max_bytes,
        retries=0,
    )
    # GDELT отвечает 11–16 с и требует не чаще одного запроса в 5 с (диагностика стенда 29.09: 429 «Please limit
    # requests to one every 5 seconds»). Без внутренних повторов клиента: повтор 429 через 0–2 с нарушает это правило.
    gdelt_http_client = HttpxClient(
        contact_email=settings.contact_email,
        read_timeout_seconds=max(settings.collector_http_timeout_seconds, SLOW_SOURCE_TIMEOUT_SECONDS),
        max_bytes=settings.http_max_bytes,
        retries=0,
    )
    limiter = TokenBucketRateLimiter(
        rates={entry.source_key: entry.rate_limit_rps for entry in catalog.values()},
        clock=clock,
        replicas=settings.collector_replicas,
        on_wait=metrics.rate_limit_wait,
    )
    adapters, unavailable = build_adapters(settings, catalog, http_client, limiter, clock, arxiv_http_client,
                                           gdelt_http_client)
    log.info(
        "sources.configured",
        enabled=[key.value for key in adapters],
        unavailable={key.value: code.value for key, code in unavailable.items()},
    )

    collections = PostgresCollectionRepository(pool)
    documents = PostgresDocumentRepository(pool)
    adapter_runs = PostgresAdapterRunRepository(pool)
    cache = PostgresEncyclopediaCache(pool)
    probe = WikipediaProbe(http_client, limiter, clock)

    servicer = CollectorServicer(
        start_collection=StartCollection(collections, tuple(adapters), settings.collector_max_pending),
        get_collection=GetCollection(collections, adapter_runs),
        stream_documents=StreamDocuments(collections, documents),
        get_documents=GetDocuments(documents),
        cancel_collection=CancelCollection(collections),
        check_encyclopedia=CheckEncyclopedia(cache, probe, clock, settings.encyclopedia_cache_days),
    )
    run_collection = RunCollection(
        collections=collections,
        documents=documents,
        adapter_runs=adapter_runs,
        adapters=adapters,
        classification=classification,
        clock=clock,
        config=RunCollectionConfig(
            lease_seconds=settings.collector_lease_seconds,
            heartbeat_seconds=settings.collector_heartbeat_seconds,
        ),
        unavailable_sources=unavailable,
        metrics=metrics,
    )

    server = grpc.aio.server(
        interceptors=[ObservabilityInterceptor(settings.service_name)],
        maximum_concurrent_rpcs=settings.grpc_max_concurrent_rpcs,
        options=[
            ("grpc.max_receive_message_length", settings.grpc_max_message_mb * 1024 * 1024),
            ("grpc.max_send_message_length", settings.grpc_max_message_mb * 1024 * 1024),
        ],
    )
    collector_pb2_grpc.add_CollectorServiceServicer_to_server(servicer, server)
    health_servicer = health.aio.HealthServicer()  # asyncio-сервер: set() — корутина
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f"0.0.0.0:{settings.grpc_port}")  # noqa: S104 - сеть compose без публикации порта
    await server.start()
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    ops = OpsHttpServer(settings.http_port, readiness=_readiness(pool), metrics=render_metrics)
    await ops.start()

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    purge = PurgeDocuments(documents, cache, clock, settings.document_retention_days)
    workers = [
        CollectionWorker(
            collections,
            run_collection,
            owner=f"{settings.service_name}:{index}:{id(server) & 0xFFFF:04x}",
            lease_seconds=settings.collector_lease_seconds,
        )
        for index in range(settings.collector_workers)
    ]
    tasks = [asyncio.create_task(worker.run_forever(stop)) for worker in workers]
    tasks.append(asyncio.create_task(MaintenanceWorker(collections, purge).run_forever(stop)))
    log.info("service.started", grpc_port=settings.grpc_port, http_port=settings.http_port,
             openssl=ssl.OPENSSL_VERSION)

    await stop.wait()
    log.info("service.stopping")
    await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
    await server.stop(settings.shutdown_grace_seconds)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await ops.stop()
    await http_client.aclose()
    await arxiv_http_client.aclose()
    await gdelt_http_client.aclose()
    released = await collections.release_expired_leases()
    log.info("service.stopped", leases_released=released)
    await pool.close()
    return 0


def build_adapters(
    settings: CollectorSettings,
    catalog: Mapping[SourceKey, CatalogEntry],
    http_client: HttpxClient,
    limiter: TokenBucketRateLimiter,
    clock: SystemClock,
    arxiv_http_client: HttpxClient | None = None,
    gdelt_http_client: HttpxClient | None = None,
) -> tuple[dict[SourceKey, SourceAdapter], dict[SourceKey, AdapterErrorCode]]:
    """Собирает включённые адаптеры; недоступные источники получают код отказа вместо заглушки."""
    override = settings.source_override
    adapters: dict[SourceKey, SourceAdapter] = {}
    unavailable: dict[SourceKey, AdapterErrorCode] = {}
    for source_key, entry in catalog.items():
        enabled = override.get(source_key, entry.enabled)
        if source_key is SourceKey.WIKIPEDIA:
            continue  # используется только как индикатор зрелости (CheckEncyclopedia)
        if not enabled:
            unavailable[source_key] = AdapterErrorCode.DISABLED
            continue
        try:
            adapter = _create_adapter(source_key, settings, http_client, limiter, clock, arxiv_http_client,
                                      gdelt_http_client)
        except ValueError as exc:
            unavailable[source_key] = (
                AdapterErrorCode.AUTH_MISSING if entry.requires_api_key else AdapterErrorCode.DISABLED
            )
            log.warning("source.unavailable", source=source_key.value, reason=str(exc))
            continue
        if adapter is None:
            unavailable[source_key] = AdapterErrorCode.DISABLED
            continue
        adapters[source_key] = adapter
    return adapters, unavailable


def _create_adapter(
    source_key: SourceKey,
    settings: CollectorSettings,
    http_client: HttpxClient,
    limiter: TokenBucketRateLimiter,
    clock: SystemClock,
    arxiv_http_client: HttpxClient | None = None,
    gdelt_http_client: HttpxClient | None = None,
) -> SourceAdapter | None:
    """Создаёт адаптер источника; ValueError означает отсутствие обязательной настройки."""
    if source_key is SourceKey.OPENALEX:
        return OpenAlexAdapter(http_client, limiter, clock, settings.openalex_api_key)
    if source_key is SourceKey.ARXIV:
        return ArxivAdapter(arxiv_http_client or http_client, limiter, clock)
    if source_key is SourceKey.RSS:
        return RssAdapter(http_client, limiter, clock, settings.rss_feeds)
    if source_key is SourceKey.GITHUB:
        return GithubAdapter(http_client, limiter, clock, settings.github_token)
    if source_key is SourceKey.SEMANTIC_SCHOLAR:
        return SemanticScholarAdapter(http_client, limiter, clock, settings.semantic_scholar_api_key)
    if source_key is SourceKey.ZENODO:
        return ZenodoAdapter(http_client, limiter, clock)
    if source_key is SourceKey.GDELT:
        return GdeltAdapter(gdelt_http_client or http_client, limiter, clock)
    if source_key is SourceKey.ROSPATENT:
        return RospatentAdapter(http_client, limiter, clock, settings.rospatent_token)
    return None  # patentsview и hh включаются после подтверждения условий API (§6.5 ТЗ)


def _readiness(pool: object) -> object:
    """Проверка готовности: доступность PostgreSQL."""

    async def check() -> bool:
        try:
            async with pool.connection() as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
                await cur.execute("SELECT 1")
        except Exception:  # noqa: BLE001 - любая ошибка означает «не готов»
            return False
        return True

    return check


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT → корректная остановка (§9 COMMON)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


def main(argv: list[str]) -> int:
    """Точка входа CLI."""
    if len(argv) != 2 or argv[1] != "serve":
        print("использование: python -m collector.main serve", file=sys.stderr)
        return 2
    settings = load_settings(CollectorSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    return asyncio.run(serve(settings))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
