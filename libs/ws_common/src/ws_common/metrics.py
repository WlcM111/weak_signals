"""Метрики Prometheus, общие для сервисов (§14.6 ТЗ)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

GRPC_REQUESTS = Counter(
    "ws_grpc_requests_total", "Обработанные gRPC-запросы", ["service", "rpc", "code"], registry=REGISTRY
)
GRPC_LATENCY = Histogram(
    "ws_grpc_latency_seconds", "Длительность gRPC-запросов", ["service", "rpc"], registry=REGISTRY
)
ADAPTER_REQUESTS = Counter(
    "ws_adapter_requests_total", "HTTP-запросы адаптеров источников", ["source", "status"], registry=REGISTRY
)
DOCUMENTS_NEW = Counter("ws_documents_new_total", "Новые документы", ["source"], registry=REGISTRY)
RATE_LIMIT_WAITS = Counter(
    "ws_rate_limit_waits_total", "Ожидания token bucket", ["source"], registry=REGISTRY
)
COLLECTION_DURATION = Histogram(
    "ws_collection_duration_seconds",
    "Длительность сбора",
    ["mode"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
COLLECTIONS = Counter("ws_collections_total", "Завершённые коллекции", ["status"], registry=REGISTRY)

EMBEDDINGS_COMPUTED = Counter(
    "ws_embeddings_computed_total", "Вычисленные эмбеддинги документов", registry=REGISTRY
)
EMBEDDING_CACHE_HITS = Counter(
    "ws_embedding_cache_hits_total", "Попадания в кеш эмбеддингов", registry=REGISTRY
)
ANALYSIS_STEP_DURATION = Histogram(
    "ws_analysis_duration_seconds",
    "Длительность шагов анализа",
    ["step"],
    buckets=(0.05, 0.25, 1, 5, 15, 30, 60, 120),
    registry=REGISTRY,
)
CANDIDATES = Counter("ws_candidates_total", "Кандидаты по решениям", ["decision"], registry=REGISTRY)
ANALYSES = Counter("ws_analyses_total", "Завершённые анализы", ["status"], registry=REGISTRY)
MODEL_INFO = Gauge("ws_model_info", "Активная версия модели", ["version"], registry=REGISTRY)

JOBS = Counter("ws_jobs_total", "Завершённые задания", ["status"], registry=REGISTRY)
JOB_DURATION = Histogram(
    "ws_job_duration_seconds",
    "Длительность стадий задания",
    ["stage"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
QUEUE_PENDING = Gauge("ws_queue_pending", "Заданий в очереди и в работе", registry=REGISTRY)
HTTP_REQUESTS = Counter(
    "ws_http_requests_total", "Запросы к HTTP API", ["path", "status"], registry=REGISTRY
)
UPSTREAM_CALLS = Counter(
    "ws_upstream_calls_total", "Вызовы внутренних сервисов", ["rpc", "code"], registry=REGISTRY
)

LLM_CALLS = Counter(
    "ws_llm_calls_total", "Вызовы языковых моделей", ["provider", "status"], registry=REGISTRY
)
LLM_TOKENS = Counter(
    "ws_llm_tokens_total", "Израсходованные токены", ["provider", "kind"], registry=REGISTRY
)
LLM_LATENCY = Histogram(
    "ws_llm_latency_seconds",
    "Длительность вызова языковой модели",
    ["provider"],
    buckets=(0.5, 1, 3, 5, 10, 20, 40, 60, 120),
    registry=REGISTRY,
)
INSIGHT_STATUS = Counter(
    "ws_insight_status_total", "Инсайты по статусам", ["status"], registry=REGISTRY
)
PROVIDER_HEALTHY = Gauge(
    "ws_provider_healthy", "Доступность провайдера LLM", ["provider"], registry=REGISTRY
)
LLM_DAILY_TOKENS = Gauge("ws_llm_daily_tokens", "Токены за текущие сутки", registry=REGISTRY)


def render_metrics() -> bytes:
    """Текст экспозиции Prometheus для `GET /metrics`."""
    return generate_latest(REGISTRY)
