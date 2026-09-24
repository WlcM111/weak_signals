"""Composition root insight: `python -m insight.main serve` (§9 HANDOFF)."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from insight.adapters.inbound.grpc_server import InsightServicer
from insight.adapters.inbound.http_ops import OpsHttpServer
from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.adapters.outbound.llm.gigachat_provider import GigaChatProvider
from insight.adapters.outbound.llm.openai_compat_provider import OpenAiCompatProvider
from insight.adapters.outbound.metrics import PrometheusMetrics
from insight.adapters.outbound.postgres.repositories import (
    PostgresExpansionRepository,
    PostgresInsightRepository,
    PostgresLLMCallLog,
    PostgresPromptRegistry,
)
from insight.application.ports import LLMProvider
from insight.application.prompt_builder import PromptBuilder
from insight.application.provider_chain import ChainConfig, ProviderChain
from insight.application.use_cases.expand_query import ExpandQuery, Glossary
from insight.application.use_cases.generate_insight import GenerateConfig, GenerateInsight
from insight.application.use_cases.get_provider_status import GetProviderStatus, RegisterPrompts
from insight.config import InsightSettings
from ws_common.clock import SystemClock
from ws_common.config import load_settings
from ws_common.db import build_pool
from ws_common.grpc_interceptors import ObservabilityInterceptor
from ws_common.logging import configure_logging, get_logger
from ws_common.migrate import apply_migrations

SCHEMA = "insight"
HEALTHCHECK_INTERVAL_SECONDS = 60.0
log = get_logger("insight.main")


def build_providers(settings: InsightSettings) -> list[LLMProvider]:
    """Создаёт провайдеров в порядке цепочки; ненастроенные пропускаются с предупреждением."""
    providers: list[LLMProvider] = []
    for name in settings.provider_order:
        provider = _build_provider(name, settings)
        if provider is None:
            log.warning("provider.state", provider=name, healthy=False, reason="not_configured")
            continue
        providers.append(provider)
    if not providers:
        log.warning("provider.state", provider="none", healthy=False, reason="no_providers")
    return providers


def _build_provider(name: str, settings: InsightSettings) -> LLMProvider | None:
    """Один провайдер по имени и конфигурации."""
    if name == "gigachat" and settings.gigachat_credentials:
        return GigaChatProvider(
            credentials=settings.gigachat_credentials,
            model=settings.gigachat_model,
            scope=settings.gigachat_scope,
            ca_bundle_file=settings.gigachat_ca_bundle,
            max_concurrency=settings.gigachat_max_concurrency,
        )
    if name == "yandexgpt" and settings.yandex_api_key and settings.yandex_folder_id:
        return OpenAiCompatProvider(
            name="yandexgpt",
            base_url=settings.yandex_base_url,
            model=f"gpt://{settings.yandex_folder_id}/{settings.yandex_model}",
            api_key=settings.yandex_api_key,
            auth_scheme="Api-Key",
            max_concurrency=settings.yandex_max_concurrency,
        )
    if name == "local_llamacpp":
        return OpenAiCompatProvider(
            name="local_llamacpp",
            base_url=settings.local_llm_base_url,
            model=settings.local_llm_model,
            max_concurrency=1,
        )
    if name == "fake" and settings.env == "test":
        return FakeProvider()
    return None


async def serve(settings: InsightSettings) -> int:
    """Поднимает gRPC-сервер, эндпоинты обслуживания и фоновую проверку провайдеров."""
    import grpc  # noqa: PLC0415 - тяжёлая зависимость рабочего контура
    from grpc_health.v1 import health, health_pb2, health_pb2_grpc  # noqa: PLC0415
    from weaksignals.insight.v1 import insight_pb2_grpc  # noqa: PLC0415

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

    prompts = PromptBuilder(settings.prompts_dir, settings.schemas_dir)
    await RegisterPrompts(PostgresPromptRegistry(pool), prompts).execute()
    providers = build_providers(settings)
    chain = ProviderChain(
        providers,
        PostgresLLMCallLog(pool),
        clock,
        ChainConfig(
            timeout_seconds=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
            circuit_breaker_failures=settings.circuit_breaker_failures,
            circuit_breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
            daily_token_budget=settings.llm_daily_token_budget,
            total_budget_seconds=settings.llm_total_budget_seconds,
        ),
        metrics,
    )
    servicer = InsightServicer(
        expand_query=ExpandQuery(
            chain,
            prompts,
            PostgresExpansionRepository(pool),
            Glossary.load(settings.glossary_path),
            metrics,
        ),
        generate_insight=GenerateInsight(
            chain,
            prompts,
            PostgresInsightRepository(pool),
            clock,
            GenerateConfig(
                max_attempts=settings.llm_max_attempts,
                cache_days=settings.insight_cache_days,
                min_features=settings.grounding_min_features,
                min_cyrillic_share=settings.grounding_min_cyrillic_share,
            ),
            metrics,
        ),
        provider_status=GetProviderStatus(chain),
        max_queue=settings.insight_max_queue,
    )

    server = grpc.aio.server(
        interceptors=[ObservabilityInterceptor(settings.service_name)],
        maximum_concurrent_rpcs=32,
        options=[
            ("grpc.max_receive_message_length", settings.grpc_max_message_mb * 1024 * 1024),
            ("grpc.max_send_message_length", settings.grpc_max_message_mb * 1024 * 1024),
        ],
    )
    insight_pb2_grpc.add_InsightServiceServicer_to_server(servicer, server)
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f"0.0.0.0:{settings.grpc_port}")  # noqa: S104 - сеть compose
    await server.start()
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    ops = OpsHttpServer(settings.http_port, readiness=lambda: True)
    await ops.start()
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    health_task = asyncio.create_task(_watch_providers(providers, chain, metrics, stop))
    log.info(
        "service.started",
        grpc_port=settings.grpc_port,
        http_port=settings.http_port,
        providers=[provider.name for provider in providers],
    )
    await stop.wait()
    log.info("service.stopping")
    health_task.cancel()
    await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
    await server.stop(settings.shutdown_grace_seconds)
    await ops.stop()
    await pool.close()
    log.info("service.stopped")
    return 0


async def _watch_providers(
    providers: list[LLMProvider], chain: ProviderChain, metrics: Any, stop: asyncio.Event
) -> None:
    """Фоновая проверка провайдеров: только для тех, что помечены недоступными (§9 HANDOFF)."""
    while not stop.is_set():
        unhealthy = {state.provider for state in chain.states() if not state.healthy}
        for provider in providers:
            if provider.name not in unhealthy:
                continue
            healthy = await provider.healthcheck()
            metrics.provider_healthy(provider.name, healthy)
            log.info("provider.state", provider=provider.name, healthy=healthy, reason="probe")
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEALTHCHECK_INTERVAL_SECONDS)
        except TimeoutError:
            continue


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT → корректная остановка."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


def main(argv: list[str]) -> int:
    """Точка входа CLI."""
    if len(argv) < 2 or argv[1] != "serve":
        print("использование: python -m insight.main serve", file=sys.stderr)
        return 2
    settings = load_settings(InsightSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    return asyncio.run(serve(settings))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
