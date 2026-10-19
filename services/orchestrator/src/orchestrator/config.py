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
    # Этап 4: сбор в два прохода — широкий по фразам темы и прицельный по технологиям, поэтому 240 с.
    # Этап 4.7: 480 (было 240) — в 5 темах из 6 у всех 13 источников DEADLINE_REACHED.
    # Этап 5.0.4: границы — как у контракта collector (10..600 с, 1..3000 документов): значение вне них collector
    # отклонял в каждом задании, теперь ошибка видна при запуске orchestrator.
    collect_time_budget_seconds: int = Field(default=480, ge=10, le=600)
    # Этап 4.4: 1200 (было 800) — в 4 темах из 6 сбор упирался в лимит и прицельный проход обрезался.
    # Этап 4.7: 1450 (было 1200) — +250 мест под канал событий 400 вместо 150, остальные источники не теснятся.
    # Этап 5.0.1: 3000 (было 1450) — в 5.0 лимит заканчивал сбор за ~110 с во всех 6 темах, архив занимал половину
    # и вытеснял остальные источники; analyzer принимает до 3000 документов (WS_ANALYZER_MAX_DOCUMENTS).
    collect_max_total_documents: int = Field(default=3000, ge=10, le=3000)
    min_documents: int = Field(default=20, ge=1, le=1000)
    # Этап 5.0 (N04): ожидание анализа растёт с числом документов — max(базы, документов × секунды на документ).
    # Этап 5.0.3: 0 — отдельного лимита анализа нет, анализ ограничен только сроком задания (за вычетом резерва на
    # карточки). Фиксированное ожидание (300 → 728 с) обрывало анализ, который шёл и успевал в срок задания.
    analyze_timeout_seconds: int = Field(default=0, ge=0, le=14400)
    analyze_seconds_per_document: float = Field(default=0.5, ge=0.0, le=10.0)
    # N02: сколько кандидатов запрашивать у analyzer в режиме legacy (в rubric — WS_RUBRIC_POOL).
    analyzer_max_candidates: int = Field(default=150, ge=5, le=500)
    insight_concurrency: int = Field(default=1, ge=1, le=8)
    insight_timeout_seconds: int = Field(default=90, ge=10, le=300)
    # Включение сквозного срока задания: false — срок не проверяется, задание работает без ограничения по времени.
    job_deadline_enabled: bool = True
    # Сквозной срок одного задания от приёма до сохранённого результата (требование: не более 20 минут).
    # Этап 5.0: срок задания 3600 с — точность и полнота важнее 20 минут на ответ.
    job_deadline_seconds: int = Field(default=7200, ge=60, le=28800)  # этап 5.0.3: 2 часа на задание
    # Резерв до срока на запись результата и завершение задания.
    job_deadline_reserve_seconds: int = Field(default=120, ge=10, le=900)
    # Смысловая оценка кандидатов моделью перед нарративом и размер оцениваемого пула.
    candidate_judge_enabled: bool = True
    candidate_judge_pool: int = Field(default=60, ge=5, le=200)  # этап 5.0 (N13): было ≤ 40
    # ml — порядок выдачи задаёт локальная модель, смысловая оценка только исключает; llm — прежний порядок.
    candidate_judge_order: str = Field(default="ml", pattern="^(ml|llm)$")
    # Режим отбора выдачи: legacy — прежний (порог модели analyzer + смысловая оценка); rubric — рубричная оценка
    # LLM с полным контекстом источников, балл «стадия + тренд», пакетная доводка карточек. Откат — legacy.
    selection_mode: str = Field(default="legacy", pattern="^(legacy|rubric)$")
    # Пул рубричной оценки; в режиме rubric столько же кандидатов запрашивается у analyzer (v14: 40 → 80, до 100).
    rubric_pool: int = Field(default=150, ge=5, le=500)  # этап 5.0 (N13): было ≤ 100
    rubric_pool_exclude_model_mature: bool = False
    # U-02: одиночные научные работы, препринты и патенты — в пул рубрики (ранние исследования — слабые сигналы)
    rubric_pool_research_singles: bool = True
    # Этап 5.3: верхняя половина кандидатов по оценке модели analyzer — первой в выдаче (0 — прежний порядок)
    rank_model_first_share: float = Field(default=0.5, ge=0.0, le=1.0)
    rubric_docs_retry: bool = False
    finalize_enabled: bool = True
    # R-026: сбой FinalizeCards — повтор частями ≤ 5, без карточки — в очередь следующего раунда; ≤ 30 за вызов
    rubric_finalize_retry_small: bool = False
    rubric_legacy_fallback: bool = False
    rubric_fill_uncertain: bool = True
    stage_calibration: str = Field(default="1:2,2:3,3:4,4:4", pattern=r"^(\d:\d)(,\d:\d)*$")
    trend_calibration: str = Field(default="1:2,2:3,3:3", pattern=r"^(\d:\d)(,\d:\d)*$")
    rubric_model_path: str = str(SERVICE_ROOT / "config" / "rubric_ranker.json")
    rubric_min_probability: float = Field(default=0.5, ge=0.0, le=1.0)
    signal_model_path: str = str(SERVICE_ROOT / "config" / "signal_classifier.json")
    profile_model_path: str = str(SERVICE_ROOT / "config" / "profile_classifier.json")
    final_specificity_check: bool = False
    # R-025: без вердикта финальной проверки (сбой, пропуск) — «требует проверки», как U; резервные — через неё же
    final_check_fail_as_uncertain: bool = False
    confidence_calibration: bool = False
    # R-003: документы (URL) и итог каждого кандидата в трассе narrate; прошлые прогоны стадии — narrate@N
    narrate_trace_evidence: bool = False
    # R-003: GET /api/v1/jobs/{job_id}/trace за API-ключом; false — маршрута нет
    trace_api: bool = False
    rubric_min_cards: int = Field(default=3, ge=0, le=15)
    evidence_text_max_chars: int = Field(default=2000, ge=200, le=8000)
    prompt_version: str = "insight_v1"
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=168)
    retention_interval_minutes: int = Field(default=60, ge=1, le=1440)

    # R-049: строгий резервный перевод запроса без LLM; по умолчанию прежнее поведение.
    query_fallback_strict: bool = False
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
