"""Конфигурация orchestrator: переменные `WS_*` (§12 HANDOFF)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from ws_common.config import BaseServiceSettings

SERVICE_ROOT = Path(__file__).resolve().parents[2]


class OrchestratorSettings(BaseServiceSettings):
    """Параметры сервиса; неверная комбинация приводит к отказу запуска."""

    service_name: str = "orchestrator"
    http_port: int = Field(default=8080, ge=1024, le=65535)
    api_bind: str = "127.0.0.1"
    # Адрес, на котором API доступен снаружи. В Docker это хостовая сторона публикации порта (compose.yaml),
    # а api_bind — адрес внутри контейнера. Пусто — совпадает с api_bind (запуск без контейнера).
    api_public_bind: str = ""
    api_key: str = ""
    ip_hash_salt: str = "weak-signals"
    rate_limit_post_per_min: int = Field(default=30, ge=0, le=1000)
    queue_max_pending: int = Field(default=3, ge=1, le=100)
    pg_user: str = "ws_orchestrator"

    collector_addr: str = "collector:50051"
    analyzer_addr: str = "analyzer:50052"
    insight_addr: str = "insight:50053"
    insight_enabled: bool = True

    worker_concurrency: int = Field(default=1, ge=1, le=8)
    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    job_lease_seconds: int = Field(default=60, ge=15, le=600)
    job_heartbeat_seconds: int = Field(default=20, ge=5, le=120)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    stage_poll_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    collect_time_budget_seconds: int = Field(default=120, ge=10, le=1800)
    collect_max_total_documents: int = Field(default=800, ge=10, le=5000)
    min_documents: int = Field(default=20, ge=1, le=1000)
    analyze_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    insight_concurrency: int = Field(default=1, ge=1, le=8)
    insight_timeout_seconds: int = Field(default=90, ge=10, le=300)
    # Включение сквозного срока задания: false — срок не проверяется, задание работает без ограничения по времени.
    job_deadline_enabled: bool = True
    # Сквозной срок одного задания от приёма до сохранённого результата (требование: не более 20 минут).
    job_deadline_seconds: int = Field(default=1200, ge=60, le=7200)
    # Резерв до срока на запись результата и завершение задания.
    job_deadline_reserve_seconds: int = Field(default=120, ge=10, le=900)
    # Смысловая оценка кандидатов моделью перед нарративом и размер оцениваемого пула.
    candidate_judge_enabled: bool = True
    candidate_judge_pool: int = Field(default=30, ge=5, le=40)
    # ml — порядок выдачи задаёт локальная модель, смысловая оценка только исключает; llm — прежний порядок.
    candidate_judge_order: str = Field(default="ml", pattern="^(ml|llm)$")
    # Режим отбора выдачи: legacy — прежний (порог модели analyzer + смысловая оценка); rubric — рубричная оценка
    # LLM с полным контекстом источников, балл «стадия + тренд», пакетная доводка карточек. Откат — legacy.
    selection_mode: str = Field(default="legacy", pattern="^(legacy|rubric)$")
    rubric_pool: int = Field(default=40, ge=5, le=40)
    finalize_enabled: bool = True
    rubric_legacy_fallback: bool = False
    rubric_fill_uncertain: bool = True
    stage_calibration: str = Field(default="1:2,2:3,3:4,4:4", pattern=r"^(\d:\d)(,\d:\d)*$")
    trend_calibration: str = Field(default="1:2,2:3,3:3", pattern=r"^(\d:\d)(,\d:\d)*$")
    rubric_model_path: str = str(SERVICE_ROOT / "config" / "rubric_ranker.json")
    rubric_min_probability: float = Field(default=0.5, ge=0.0, le=1.0)
    signal_model_path: str = str(SERVICE_ROOT / "config" / "signal_classifier.json")
    profile_model_path: str = str(SERVICE_ROOT / "config" / "profile_classifier.json")
    final_specificity_check: bool = False
    confidence_calibration: bool = False
    rubric_min_cards: int = Field(default=3, ge=0, le=15)
    evidence_text_max_chars: int = Field(default=2000, ge=200, le=8000)
    prompt_version: str = "insight_v1"
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=168)
    retention_interval_minutes: int = Field(default=60, ge=1, le=1440)

    glossary_path: Path = SERVICE_ROOT / "config" / "glossary_ru_en.yaml"
    migrations_dir: Path = SERVICE_ROOT / "migrations"

    @model_validator(mode="after")
    def check_public_bind_requires_key(self) -> OrchestratorSettings:
        """Публичная привязка без API-ключа запрещена (§12 HANDOFF)."""
        exposed = self.api_public_bind or self.api_bind
        if exposed not in {"127.0.0.1", "localhost", "::1"} and not self.api_key:
            raise ValueError(
                "WS_API_BIND открывает сервис наружу: задайте WS_API_KEY или привяжите к 127.0.0.1"
            )
        return self
