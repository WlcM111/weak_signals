"""Конфигурация сервиса collector: переменные окружения `WS_*` (§12 HANDOFF, словарь §15.4)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator

from collector.config_parsing import parse_feed_list, parse_source_override
from collector.domain.values import SourceKey
from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]


class CollectorSettings(BaseServiceSettings):
    """Параметры collector; неверное значение приводит к отказу запуска."""

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
