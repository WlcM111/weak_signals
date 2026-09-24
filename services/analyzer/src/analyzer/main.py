"""Composition root сервиса analyzer: `python -m analyzer.main serve|register-model <path>` (§9 HANDOFF)."""

from __future__ import annotations

import os
import signal
import sys
import threading
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from weaksignals.analyzer.v1 import analyzer_pb2_grpc

from analyzer.adapters.inbound.grpc_server import AnalyzerServicer
from analyzer.adapters.inbound.http_ops import OpsHttpServer
from analyzer.adapters.inbound.worker import AnalysisWorker, LeaseSweeper, ModelWatcher
from analyzer.adapters.outbound.collector_grpc import CollectorGrpcClient
from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.adapters.outbound.e5_embedder import E5Embedder
from analyzer.adapters.outbound.metrics import PrometheusMetrics
from analyzer.adapters.outbound.model_store import FileSystemModelStore, ModelStoreError
from analyzer.adapters.outbound.postgres.analysis_repository import PostgresAnalysisRepository
from analyzer.adapters.outbound.postgres.candidate_repository import PostgresCandidateRepository
from analyzer.adapters.outbound.postgres.embedding_cache import PostgresEmbeddingCache
from analyzer.adapters.outbound.postgres.model_version_repository import PostgresModelVersionRepository
from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.use_cases.activate_model import ActivateModelFromStore
from analyzer.application.use_cases.cancel_analysis import CancelAnalysis
from analyzer.application.use_cases.get_analysis import GetAnalysis
from analyzer.application.use_cases.get_model_info import GetModelInfo
from analyzer.application.use_cases.list_candidates import ListCandidates
from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig
from analyzer.application.use_cases.score_text import ScoreText
from analyzer.application.use_cases.start_analysis import StartAnalysis
from analyzer.config import AnalyzerSettings
from ws_common.clock import SyncSystemClock
from ws_common.config import load_settings
from ws_common.db import build_sync_pool
from ws_common.grpc_clients import build_channel
from ws_common.grpc_interceptors import SyncObservabilityInterceptor
from ws_common.ids import worker_id
from ws_common.logging import configure_logging, get_logger
from ws_common.metrics import render_metrics
from ws_common.migrate import apply_migrations_sync

SCHEMA = "analyzer"
log = get_logger("analyzer.main")


def serve(settings: AnalyzerSettings) -> int:
    """Поднимает gRPC-сервер, воркеры анализов и HTTP-эндпоинты обслуживания."""
    os.environ.setdefault("HF_HOME", str(settings.hf_home))
    clock = SyncSystemClock()
    metrics = PrometheusMetrics()
    registry = load_feature_registry(settings.feature_registry_path)
    lexicons = load_lexicons(settings.lexicon_dir, settings.stage_rules_path)
    pool = build_sync_pool(
        settings.pg_dsn,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        statement_timeout_ms=settings.pg_statement_timeout_ms,
        application_name=settings.service_name,
    )
    pool.open(wait=True, timeout=30)
    applied = apply_migrations_sync(pool, SCHEMA, settings.migrations_dir)
    log.info("migrations.applied", versions=applied)

    holder = ActiveModelHolder()
    embedder = E5Embedder(
        model_name=settings.embedding_model,
        batch_size=settings.embedding_batch_size,
        torch_threads=settings.torch_threads,
        cache_dir=str(settings.hf_home),
    )
    activate = ActivateModelFromStore(
        store=FileSystemModelStore(settings.model_store_dir, settings.model_require_sha256),
        registry=registry,
        versions=PostgresModelVersionRepository(pool),
        holder=holder,
        embedder=embedder,
        metrics=metrics,
    )
    try:
        activate.execute()
    except (ModelStoreError, Exception) as error:  # noqa: BLE001 - сервис остаётся не готов
        log.error("model.corrupt", error=str(error))

    channel = build_channel(settings.collector_addr, caller=settings.service_name)
    collector = CollectorGrpcClient(channel)
    analyses = PostgresAnalysisRepository(pool)
    candidates = PostgresCandidateRepository(
        pool, {spec.name: spec.label_ru for spec in registry.features}
    )
    servicer = AnalyzerServicer(
        start_analysis=StartAnalysis(analyses, collector, holder, settings.analyzer_max_pending),
        get_analysis=GetAnalysis(analyses),
        list_candidates=ListCandidates(analyses, candidates),
        cancel_analysis=CancelAnalysis(analyses),
        score_text=ScoreText(
            collector, embedder, holder, lexicons, clock, settings.enrichment_timeout_seconds, metrics
        ),
        get_model_info=GetModelInfo(holder),
    )
    run_analysis = RunAnalysis(
        analyses=analyses,
        candidates=candidates,
        embedding_cache=PostgresEmbeddingCache(pool),
        collector=collector,
        embedder=embedder,
        active_model=holder,
        lexicons=lexicons,
        clock=clock,
        config=RunAnalysisConfig(
            lease_seconds=settings.analyzer_lease_seconds,
            heartbeat_seconds=settings.analyzer_heartbeat_seconds,
            max_documents=settings.analyzer_max_documents,
            stream_chunk_size=settings.analyzer_stream_chunk_size,
            embedding_batch_size=settings.embedding_batch_size,
            min_doc_query_sim=settings.min_doc_query_sim,
            min_candidate_query_sim=settings.min_candidate_query_sim,
            trust_weighted_selection=settings.trust_weighted_selection,
            require_high_trust_source=settings.require_high_trust_source,
            near_dup_threshold=settings.near_dup_threshold,
            cluster_distance_threshold=settings.cluster_distance_threshold,
            min_cluster_size=settings.min_cluster_size,
            evidence_max=settings.evidence_max,
        ),
        metrics=metrics,
    )

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.grpc_workers),
        interceptors=[SyncObservabilityInterceptor(settings.service_name)],
        maximum_concurrent_rpcs=settings.grpc_max_concurrent_rpcs,
        options=[
            ("grpc.max_receive_message_length", settings.grpc_max_message_mb * 1024 * 1024),
            ("grpc.max_send_message_length", settings.grpc_max_message_mb * 1024 * 1024),
        ],
    )
    analyzer_pb2_grpc.add_AnalyzerServiceServicer_to_server(servicer, server)
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f"0.0.0.0:{settings.grpc_port}")  # noqa: S104 - сеть compose
    server.start()
    # gRPC health = «процесс принимает вызовы». Готовность модели отражают /readyz и код
    # MODEL_NOT_LOADED: иначе compose не поднимал orchestrator и ui, пока модель не обучена,
    # а обучить её можно только после старта collector.
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    ops = OpsHttpServer(settings.http_port, readiness=lambda: holder.is_loaded, metrics=render_metrics)
    ops.start()

    stop = threading.Event()
    _install_signal_handlers(stop)
    threads = [
        threading.Thread(
            target=AnalysisWorker(
                analyses, run_analysis, worker_id(settings.service_name), settings.analyzer_lease_seconds
            ).run_forever,
            args=(stop,),
            name=f"analysis-worker-{index}",
            daemon=True,
        )
        for index in range(settings.analyzer_workers)
    ]
    threads.append(
        threading.Thread(
            target=LeaseSweeper(analyses).run_forever, args=(stop,), name="lease-sweeper", daemon=True
        )
    )
    threads.append(
        threading.Thread(
            target=ModelWatcher(
                activate.execute,
                lambda: holder.is_loaded,
                settings.model_store_dir / "active" / "manifest.json",
            ).run_forever,
            args=(stop,),
            name="model-watcher",
            daemon=True,
        )
    )
    for thread in threads:
        thread.start()
    log.info(
        "service.started",
        grpc_port=settings.grpc_port,
        http_port=settings.http_port,
        model_loaded=holder.is_loaded,
    )

    stop.wait()
    log.info("service.stopping")
    health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
    server.stop(settings.shutdown_grace_seconds).wait(settings.shutdown_grace_seconds)
    for thread in threads:
        thread.join(timeout=5)
    ops.stop()
    channel.close()
    pool.close()
    log.info("service.stopped")
    return 0


def register_model(settings: AnalyzerSettings, path: str) -> int:
    """Регистрирует версию модели из `model-store` без запуска сервера."""
    registry = load_feature_registry(settings.feature_registry_path)
    pool = build_sync_pool(
        settings.pg_dsn,
        min_size=1,
        max_size=2,
        statement_timeout_ms=settings.pg_statement_timeout_ms,
        application_name=settings.service_name,
    )
    pool.open(wait=True, timeout=30)
    apply_migrations_sync(pool, SCHEMA, settings.migrations_dir)
    try:
        bundle = ActivateModelFromStore(
            store=FileSystemModelStore(settings.model_store_dir, settings.model_require_sha256),
            registry=registry,
            versions=PostgresModelVersionRepository(pool),
            holder=ActiveModelHolder(),
        ).execute(path)
    except ModelStoreError as error:
        log.error("model.corrupt", error=str(error))
        return 1
    finally:
        pool.close()
    print(f"зарегистрирована модель {bundle.version.model_version_id}")
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    """SIGTERM/SIGINT → корректная остановка."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())


def main(argv: list[str]) -> int:
    """Точка входа CLI."""
    if len(argv) < 2 or argv[1] not in {"serve", "register-model"}:
        print("использование: python -m analyzer.main serve|register-model <path>", file=sys.stderr)
        return 2
    settings = load_settings(AnalyzerSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    if argv[1] == "serve":
        return serve(settings)
    if len(argv) != 3:
        print("использование: python -m analyzer.main register-model <path>", file=sys.stderr)
        return 2
    return register_model(settings, argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
