"""Конфигурация сервиса analyzer: переменные окружения `WS_*` (§12 HANDOFF)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_ROOT.parents[1]


class AnalyzerSettings(BaseServiceSettings):
    """Параметры analyzer; неверное значение приводит к отказу запуска."""
    # Запись входов анализа для режима А (аудит A3): векторы, ответы Wikipedia, дата признаков, воронка.
    analyzer_capture_enabled: bool = True

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
    # Этап 5.0 (аудит раунда 3, N03): абсолютный порог на шкале e5 ничего не отсекал (сходство любых документов
    # 0,74–0,92), поэтому по умолчанию выключен. Отсекаются только явные выбросы: релевантность (в центрированном
    # пространстве) ниже медианы на `relevance_outlier_mad` медианных отклонений; 0 — без отсева.
    min_doc_query_sim: float = Field(default=0.0, ge=0.0, le=1.0)
    relevance_outlier_mad: float = Field(default=3.0, ge=0.0, le=10.0)
    # N01/H01: кластеризация, near-dup и отбор по релевантности — по векторам за вычетом среднего коллекции
    # (centered). Без этого косинусное сходство e5 сжато к 0,74–0,92 и порог 0,35 давал 1 кластер на 600 документов,
    # а 0,15 — сгустки по 118 и 93 документа. raw — прежнее поведение (тогда порог кластеризации около 0,10).
    vector_space: str = Field(default="centered", pattern="^(centered|raw)$")
    # Этап 5.1 (ревью e2e 5.0.4, проблема 2): фиксированный порог 0,60 дробил тему — 76–80 % одиночных кластеров.
    # adaptive: порог = квантиль расстояний до ближайшего соседа в этой коллекции (в границах min..max): примерно
    # (1 − квантиль) документов остаются одиночными. fixed — прежний WS_CLUSTER_DISTANCE_THRESHOLD.
    cluster_threshold_mode: str = Field(default="adaptive", pattern="^(adaptive|fixed)$")
    cluster_nn_quantile: float = Field(default=0.6, ge=0.1, le=0.95)
    cluster_threshold_min: float = Field(default=0.30, ge=0.05, le=1.5)
    cluster_threshold_max: float = Field(default=0.85, ge=0.05, le=1.5)
    # Проблема 1: из 1149–1393 кластеров модель видела 150, отобранных по близости к запросу. Теперь предварительная
    # оценка модели (без проверки Wikipedia) идёт по всем кластерам, и кандидатами становятся лучшие по ней.
    preselect_by_model: bool = True
    # U-04: половина мест — лучшие по модели, половина — по близости к запросу (без повторов). Только модель (5.1) дала
    # 0 эталонных сигналов среди кандидатов при лучшей оценке 1,0 у многих кластеров; только близость (5.0.4) — 3.
    preselect_model_share: float = Field(default=0.5, ge=0.0, le=1.0)
    # Проблема 3: правило «слабо связан с запросом» — только выбросы: близость ниже медианы на N MAD по всем
    # кластерам анализа (0 — прежний абсолютный порог WS_MIN_CANDIDATE_QUERY_SIM). Абсолютный 0,88 на шкале e5
    # (0,77–0,94) помечал 89 % кандидатов, и правила зрелости, стоящие за ним, не срабатывали.
    candidate_relevance_outlier_mad: float = Field(default=3.0, ge=0.0, le=10.0)
    # Порог правила LOW_QUERY_RELEVANCE для кандидата; 0 — взять значение из rules.json модели.
    # Шкала e5 сжата к 0.8–0.95, поэтому значение модели 0.30 на практике ничего не отсекает.
    min_candidate_query_sim: float = Field(default=0.0, ge=0.0, le=1.0)
    # Отбор 40 кандидатов с учётом доверенности источников (HIGH 1.0, MEDIUM 0.5, LOW 0.25).
    trust_weighted_selection: bool = False
    # Слабый сигнал требует хотя бы одного источника высокой доверенности.
    require_high_trust_source: bool = False
    # R-023: правило 6б — только по названию кластера, Wikipedia — без вложенных n-грамм (false — прежнее поведение).
    mature_field_full_title: bool = False
    # Абляция вклада локальной модели в живых прогонах (none | constant | shuffle); штатно none.
    ml_score_ablation: str = Field(default="none", pattern="^(none|constant|shuffle)$")
    # Этап 5.0: значения для centered-пространства; одиночные документы — тоже кандидаты (min_cluster_size 1).
    near_dup_threshold: float = Field(default=0.90, ge=0.5, le=1.0)
    cluster_distance_threshold: float = Field(default=0.60, ge=0.05, le=1.5)
    min_cluster_size: int = Field(default=1, ge=1, le=50)
    keep_market_singletons: bool = False
    query_relevance_mode: str = Field(default="mean", pattern="^(mean|max)$")
    market_notes_separate: bool = False
    market_reserved_candidates: int = Field(default=0, ge=0, le=500)
    # Этап 4: одиночные документы, ближайшие к запросу, остаются кандидатами (не больше четверти пула).
    singleton_candidates: int = Field(default=0, ge=0, le=20)
    evidence_max: int = Field(default=8, ge=1, le=50)
    enrichment_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    grpc_max_concurrent_rpcs: int = Field(default=16, ge=1, le=256)
    grpc_workers: int = Field(default=4, ge=1, le=32)

    feature_registry_path: Path = REPO_ROOT / "schemas" / "feature_registry_v1.json"
    lexicon_dir: Path = SERVICE_ROOT / "config" / "lexicons"
    stage_rules_path: Path = SERVICE_ROOT / "config" / "stage_rules.yaml"
    migrations_dir: Path = SERVICE_ROOT / "migrations"
