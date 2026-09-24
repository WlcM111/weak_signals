"""Конфигурация сервиса analyzer: переменные окружения `WS_*` (§12 HANDOFF)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parents[1]


class AnalyzerSettings(BaseServiceSettings):
    """Параметры analyzer; неверное значение приводит к отказу запуска."""

    service_name: str = "analyzer"
    grpc_port: int = Field(default=50052, ge=1024, le=65535)
    http_port: int = Field(default=8082, ge=1024, le=65535)
    pg_user: str = "ws_analyzer"

    collector_addr: str = "collector:50051"
    model_store_dir: Path = Path("/models")
    model_require_sha256: bool = True
    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    torch_threads: int = Field(default=4, ge=1, le=32)
    hf_home: Path = Path("/hf-cache")

    analyzer_workers: int = Field(default=1, ge=1, le=8)
    analyzer_max_pending: int = Field(default=20, ge=1, le=500)
    analyzer_lease_seconds: int = Field(default=60, ge=15, le=600)
    analyzer_heartbeat_seconds: int = Field(default=20, ge=5, le=120)
    analyzer_max_documents: int = Field(default=3000, ge=10, le=20000)
    analyzer_stream_chunk_size: int = Field(default=200, ge=1, le=200)
    min_doc_query_sim: float = Field(default=0.25, ge=0.0, le=1.0)
    # Порог правила LOW_QUERY_RELEVANCE для кандидата; 0 — взять значение из rules.json модели.
    # Шкала e5 сжата к 0.8–0.95, поэтому значение модели 0.30 на практике ничего не отсекает.
    min_candidate_query_sim: float = Field(default=0.0, ge=0.0, le=1.0)
    # Отбор 40 кандидатов с учётом доверенности источников (HIGH 1.0, MEDIUM 0.5, LOW 0.25).
    trust_weighted_selection: bool = False
    # Слабый сигнал требует хотя бы одного источника высокой доверенности.
    require_high_trust_source: bool = False
    near_dup_threshold: float = Field(default=0.92, ge=0.5, le=1.0)
    cluster_distance_threshold: float = Field(default=0.35, ge=0.05, le=1.0)
    min_cluster_size: int = Field(default=2, ge=1, le=50)
    evidence_max: int = Field(default=8, ge=1, le=50)
    enrichment_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    grpc_max_concurrent_rpcs: int = Field(default=16, ge=1, le=256)
    grpc_workers: int = Field(default=4, ge=1, le=32)

    feature_registry_path: Path = REPO_ROOT / "schemas" / "feature_registry_v1.json"
    lexicon_dir: Path = SERVICE_ROOT / "config" / "lexicons"
    stage_rules_path: Path = SERVICE_ROOT / "config" / "stage_rules.yaml"
    migrations_dir: Path = SERVICE_ROOT / "migrations"
