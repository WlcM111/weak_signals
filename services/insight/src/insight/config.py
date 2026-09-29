"""Конфигурация insight: переменные `WS_*` (§12 HANDOFF).

Проверки запуска строгие: модель вне списка ТЗ и провайдер `fake` вне тестового окружения
приводят к отказу старта, а не к тихой подмене поведения.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from insight.adapters.outbound.llm.gigachat_provider import ALLOWED_MODELS
from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_PROVIDERS = ("gigachat", "yandexgpt", "local_llamacpp", "fake", "none")


class InsightSettings(BaseServiceSettings):
    """Параметры сервиса insight."""

    service_name: str = "insight"
    env: str = "prod"
    grpc_port: int = Field(default=50053, ge=1024, le=65535)
    http_port: int = Field(default=8083, ge=1024, le=65535)
    pg_user: str = "ws_insight"

    llm_primary_provider: str = "gigachat"
    llm_fallback_provider: str = "yandexgpt"
    llm_local_enabled: bool = False

    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_lite_model: str = "GigaChat-2"
    gigachat_max_concurrency: int = Field(default=1, ge=1, le=10)
    gigachat_ca_bundle: str = ""

    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_model: str = "yandexgpt/latest"
    yandex_max_concurrency: int = Field(default=2, ge=1, le=10)
    yandex_base_url: str = "https://llm.api.cloud.yandex.net/v1"

    local_llm_base_url: str = "http://localhost:8090/v1"
    local_llm_model: str = "local-model"

    # 300 с: GigaChat-2-Max отвечал на доводку дольше прежнего таймаута (ReadTimeout в 10 из 17 вызовов, стенд 29.09).
    llm_timeout_seconds: float = Field(default=300.0, ge=5.0, le=900.0)
    llm_max_attempts: int = Field(default=2, ge=1, le=5)
    llm_temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    llm_daily_token_budget: int = Field(default=0, ge=0)
    llm_total_budget_seconds: float = Field(default=600.0, ge=10.0, le=1800.0)

    circuit_breaker_failures: int = Field(default=3, ge=1, le=20)
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    insight_cache_days: int = Field(default=7, ge=1, le=90)
    insight_max_queue: int = Field(default=100, ge=1, le=1000)
    grounding_min_features: int = Field(default=2, ge=0, le=8)
    grounding_min_cyrillic_share: float = Field(default=0.6, ge=0.0, le=1.0)

    prompts_dir: Path = SERVICE_ROOT / "prompts"
    schemas_dir: Path = SERVICE_ROOT / "schemas"
    glossary_path: Path = SERVICE_ROOT / "config" / "glossary_ru_en.yaml"
    migrations_dir: Path = SERVICE_ROOT / "migrations"

    @model_validator(mode="after")
    def check_providers(self) -> InsightSettings:
        """Проверяет допустимость провайдеров и моделей до запуска сервиса."""
        for field, value in (
            ("WS_LLM_PRIMARY_PROVIDER", self.llm_primary_provider),
            ("WS_LLM_FALLBACK_PROVIDER", self.llm_fallback_provider),
        ):
            if value not in ALLOWED_PROVIDERS:
                raise ValueError(f"{field}: допустимы {', '.join(ALLOWED_PROVIDERS)}")
        if "fake" in {self.llm_primary_provider, self.llm_fallback_provider} and self.env != "test":
            raise ValueError("провайдер fake допустим только при WS_ENV=test")
        for field, model in (
            ("WS_GIGACHAT_MODEL", self.gigachat_model),
            ("WS_GIGACHAT_LITE_MODEL", self.gigachat_lite_model),
        ):
            if self._uses_gigachat and model not in ALLOWED_MODELS:
                raise ValueError(
                    f"{field}: модель {model} вне списка, разрешённого ТЗ "
                    f"({', '.join(ALLOWED_MODELS)})"
                )
        return self

    @property
    def _uses_gigachat(self) -> bool:
        """Задействован ли GigaChat в цепочке провайдеров."""
        return "gigachat" in {self.llm_primary_provider, self.llm_fallback_provider}

    @property
    def provider_order(self) -> tuple[str, ...]:
        """Порядок обращения к провайдерам: основной → резервный → локальный."""
        order = [self.llm_primary_provider, self.llm_fallback_provider]
        if self.llm_local_enabled:
            order.append("local_llamacpp")
        return tuple(name for name in dict.fromkeys(order) if name != "none")
