"""Конфигурация сервиса collector: переменные окружения `WS_*` (§12 HANDOFF, словарь §15.4)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator

from collector.config_parsing import parse_feed_list, parse_source_override
from collector.domain.source_policy import SourcePolicy, parse_source_policies
from collector.domain.values import SourceKey
from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]


class CollectorSettings(BaseServiceSettings):
    """Параметры collector; неверное значение приводит к отказу запуска."""
    # Трасса сбора для режима А (аудит A3): сырые ответы источников и события документов.
    collector_trace_enabled: bool = True

    service_name: str = "collector"
    grpc_port: int = Field(default=50051, ge=1024, le=65535)
    http_port: int = Field(default=8081, ge=1024, le=65535)
    pg_user: str = "ws_collector"

    openalex_api_key: str = ""
    github_token: str = ""
    semantic_scholar_api_key: str = ""
    patentsview_api_key: str = ""
    rospatent_token: str = ""
    collector_rss_feeds: str = ""
    contact_email: str = "team@example.org"
    collector_workers: int = Field(default=2, ge=1, le=10)
    collector_replicas: int = Field(default=1, ge=1, le=20)
    collector_max_pending: int = Field(default=50, ge=1, le=1000)
    collector_lease_seconds: int = Field(default=60, ge=15, le=600)
    collector_heartbeat_seconds: int = Field(default=20, ge=5, le=120)
    collector_http_timeout_seconds: float = Field(default=15.0, ge=3.0, le=60.0)
    collector_http_max_bytes: int = Field(default=5, ge=1, le=50)
    collector_http_retries: int = Field(default=3, ge=0, le=5)
    # Этап 3: история новостей GDELT окнами по 90 дней, таймаут соединения медленного источника, окно bulk-поиска
    # Semantic Scholar и политика бюджета/пауз по источникам (B2-b).
    gdelt_history_months: int = Field(default=24, ge=3, le=120)
    gdelt_connect_timeout_seconds: float = Field(default=20.0, ge=3.0, le=60.0)
    semantic_scholar_window_months: int = Field(default=24, ge=3, le=120)
    # Этап 3.1: история публикаций Hacker News — окна по 90 дней на эту глубину.
    hackernews_history_months: int = Field(default=24, ge=3, le=120)
    # Этап 4: прицельный проход по технологиям после широкого (см. RunCollectionConfig).
    collector_targeted_enabled: bool = True
    collector_targeted_per_term: int = Field(default=3, ge=1, le=10)
    collector_broad_time_share: float = Field(default=0.5, ge=0.2, le=0.9)
    collector_targeted_reserve_documents: int = Field(default=600, ge=0, le=2000)  # этап 4.4: было 300
    collector_broad_max_documents_per_source: int = Field(default=60, ge=10, le=500)
    collector_mined_technologies: int = Field(default=15, ge=0, le=40)
    collector_targeted_window_months: int = Field(default=24, ge=3, le=120)
    # Этап 4.3: локальные архивы, корпус arXiv и отбор технологий.
    news_publishers_path: Path = SERVICE_ROOT / "config" / "news_publishers.yaml"
    archive_wayback_pause_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    archive_wayback_from: str = Field(default="2025-01", pattern=r"^20[0-9]{2}-[0-9]{2}$")
    archive_refresh_months: int = Field(default=2, ge=0, le=12)
    news_archive_window_months: int = Field(default=24, ge=3, le=120)
    corpus_arxiv_sets: str = "cs,eess,q-fin"
    corpus_since_year: int = Field(default=2021, ge=2015, le=2030)
    corpus_oai_pause_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    collector_emerging_technologies: int = Field(default=25, ge=0, le=40)
    collector_llm_technologies: int = Field(default=10, ge=0, le=40)
    collector_snowball_technologies: int = Field(default=15, ge=0, le=40)
    collector_targeted_rounds: int = Field(default=2, ge=1, le=2)
    # Этап 4.4: бюджет документов и выборка оценки возникновения.
    collector_snowball_reserve_documents: int = Field(default=200, ge=0, le=1000)
    collector_crossref_broad_documents: int = Field(default=20, ge=0, le=500)
    collector_huggingface_broad_documents: int = Field(default=10, ge=0, le=500)
    collector_news_archive_broad_documents: int = Field(default=20, ge=0, le=500)
    collector_news_archive_per_term: int = Field(default=5, ge=1, le=20)
    collector_emergence_sample: int = Field(default=15000, ge=1000, le=50000)
    collector_news_emergence_sample: int = Field(default=10000, ge=1000, le=50000)
    # Этап 4.5: канал «события стартапов», Crossref по 1 документу на технологию, новости — только заголовки RSS.
    collector_event_channel_documents: int = Field(default=400, ge=0, le=2000)  # этап 4.7: было 150
    collector_event_min_overlap: int = Field(default=2, ge=1, le=5)
    collector_event_pool: int = Field(default=8000, ge=100, le=50000)
    collector_crossref_per_term: int = Field(default=1, ge=1, le=10)
    collector_news_emergence_origin: str = Field(default="rss", pattern=r"^(rss|wayback|)$")
    collector_news_emergence_min_rows: int = Field(default=2000, ge=0, le=100000)
    # Этап 4.6: реестр ресурсов, текст архива, квоты каналов технологий, правила возникновения.
    source_registry_path: Path = SERVICE_ROOT / "config" / "source_registry.yaml"
    archive_enrich_pause_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    archive_discover_pause_seconds: float = Field(default=1.0, ge=0.2, le=60.0)
    collector_emerging_quota: int = Field(default=15, ge=0, le=40)
    collector_mined_quota: int = Field(default=9, ge=0, le=40)
    collector_entity_quota: int = Field(default=6, ge=0, le=40)
    collector_emergence_min_growth: float = Field(default=1.5, ge=1.0, le=10.0)
    collector_emergence_min_base: int = Field(default=300, ge=0, le=100000)
    collector_source_policy: str = ""
    encyclopedia_cache_days: int = Field(default=7, ge=1, le=90)
    document_retention_days: int = Field(default=90, ge=7, le=3650)
    source_enabled_override: str = ""
    grpc_max_concurrent_rpcs: int = Field(default=64, ge=1, le=512)

    migrations_dir: Path = SERVICE_ROOT / "migrations"
    trust_rules_path: Path = SERVICE_ROOT / "config" / "trust_rules.yaml"
    rss_domains_path: Path = SERVICE_ROOT / "config" / "rss_domains.yaml"

    @field_validator("collector_rss_feeds")
    @classmethod
    def _check_feeds(cls, value: str) -> str:
        """Проверяет список лент при старте, чтобы адаптер rss не падал в рантайме."""
        if value.strip():
            parse_feed_list(value)
        return value

    @field_validator("collector_source_policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        """Проверяет политику бюджета и пауз источников при старте."""
        parse_source_policies(value)
        return value

    @property
    def source_policies(self) -> dict[SourceKey, SourcePolicy]:
        """Бюджет запросов и паузы по источникам: значения по умолчанию и переопределения из настроек."""
        return parse_source_policies(self.collector_source_policy)

    @field_validator("source_enabled_override")
    @classmethod
    def _check_override(cls, value: str) -> str:
        """Проверяет переопределение флагов каталога источников."""
        if value.strip():
            parse_source_override(value)
        return value

    @property
    def rss_feeds(self) -> tuple[str, ...]:
        """Список лент RSS (пустой, если переменная не задана)."""
        return parse_feed_list(self.collector_rss_feeds) if self.collector_rss_feeds.strip() else ()

    @property
    def source_override(self) -> dict[SourceKey, bool]:
        """Переопределение включённости источников."""
        return parse_source_override(self.source_enabled_override) if self.source_enabled_override.strip() else {}

    @property
    def http_max_bytes(self) -> int:
        """Лимит размера ответа внешнего источника в байтах."""
        return self.collector_http_max_bytes * 1024 * 1024
