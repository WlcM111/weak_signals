"""Pydantic-схемы, повторяющие `orchestrator.openapi.yaml` (документация и валидация FastAPI).

Схемы описывают тела запросов и ответов один к одному с нормативным контрактом; фактические
данные собирают `presenters.py`, поэтому схемы нужны только транспорту.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatusLiteral = Literal[
    "QUEUED", "COLLECTING", "ANALYZING", "NARRATING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED"
]
DirectionLiteral = Literal["supports_weak_signal", "supports_mature", "neutral"]


class CreateQueryRequest(BaseModel):
    """Тело `POST /api/v1/queries`."""

    query_text: str = Field(min_length=2, max_length=500)
    top_n: int = Field(default=15, ge=1, le=50)


class JobAccepted(BaseModel):
    """Ответ `202` на создание задания."""

    job_id: str
    query_id: str
    status: JobStatusLiteral
    created: bool


class JobProgressSchema(BaseModel):
    """Прогресс задания."""

    stage: JobStatusLiteral
    http_requests_total: int = 0
    sources_processed: int = 0
    documents_collected: int = 0
    candidates_found: int = 0
    narratives_done: int = 0
    narratives_total: int = 0


class ErrorSchema(BaseModel):
    """Тело ошибки (`Error`)."""

    code: str
    message: str
    request_id: str
    details: list[dict[str, str]] | None = None
    retryable: bool | None = None


class JobSchema(BaseModel):
    """Задание (`Job`)."""

    job_id: str
    query_id: str
    query_text: str
    status: JobStatusLiteral
    attempt: int
    cancel_requested: bool = False
    error: ErrorSchema | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: JobProgressSchema


class JobList(BaseModel):
    """Список заданий (`JobList`)."""

    items: list[JobSchema]
    next_cursor: str | None = None


class FeatureSchema(BaseModel):
    """Признак (`Feature`)."""

    feature_name: str
    label_ru: str
    value: float
    contribution: float
    direction: DirectionLiteral


class SourceSchema(BaseModel):
    """Источник (`Source`)."""

    document_id: str
    title: str
    url: str
    published_at: datetime | None = None
    source_type: str
    source_key: str
    language_code: str
    trust_level: Literal["HIGH", "MEDIUM", "LOW"]
    summary_ru: str
    summary_kind: Literal["ORIGINAL_RU", "GENERATIVE_SUMMARY", "EXTRACTIVE"]
    snippet: str | None = None
    similarity: float | None = None


class ResultItemSummarySchema(BaseModel):
    """Сводка элемента выдачи (`ResultItemSummary`)."""

    item_id: str
    rank: int
    title_ru: str
    score: float
    confidence_band: Literal["High", "Medium", "Low"]
    key_predictors: list[FeatureSchema]
    decision_explanation_ru: str
    narrative_status: Literal["GENERATED", "FALLBACK_EXTRACTIVE"]
    source_count: int


class ProvenanceSchema(BaseModel):
    """Происхождение нарратива и модели."""

    llm_provider: str
    llm_model: str
    prompt_version: str
    model_version_id: str


class ResultItemSchema(ResultItemSummarySchema):
    """Элемент выдачи целиком (`ResultItem`)."""

    job_id: str
    query_text: str
    title_auto: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    case_document_id: str | None = None
    explanation_ru: str
    predicted_stage: int | None = None
    predicted_trend: int | None = None
    features: list[FeatureSchema]
    sources: list[SourceSchema]
    provenance: ProvenanceSchema


class ExcludedCandidateSchema(BaseModel):
    """Исключённый кандидат (`ExcludedCandidate`)."""

    candidate_id: str
    title_auto: str
    score: float
    decision: Literal["MATURE", "HYPE_OR_NOISE", "INSUFFICIENT_EVIDENCE", "OFF_TOPIC"]
    decision_reason: str
    decision_explanation_ru: str
    document_count: int


class ResultStatsSchema(BaseModel):
    """Статистика задания (`ResultStats`)."""

    http_requests_total: int
    sources_processed: int
    documents_collected: int
    candidates_found: int
    weak_signals_total: int
    weak_signals_confident: int
    narratives_generated: int | None = None
    narratives_fallback: int | None = None
    model_version_id: str
    expand_used_fallback: bool | None = None


class ResultsSchema(BaseModel):
    """Снимок результата (`Results`)."""

    job_id: str
    query_text: str
    status: JobStatusLiteral
    items: list[ResultItemSummarySchema]
    excluded: list[ExcludedCandidateSchema]
    stats: ResultStatsSchema


class ModelInfoSchema(BaseModel):
    """Сведения о модели (`ModelInfo`)."""

    model_version_id: str
    model_family: str
    feature_schema_version: str
    embedding_model: str
    dataset_version: str
    trained_at: datetime
    metrics: dict[str, Any]
    feature_names: list[str]


class ScoreRequest(BaseModel):
    """Тело `POST /api/v1/score`."""

    title: str = Field(min_length=2, max_length=300)
    description: str = ""
    with_enrichment: bool = False


class ScoreResponse(BaseModel):
    """Ответ прямого скоринга (`ScoreResponse`)."""

    score: float
    decision: Literal[
        "WEAK_SIGNAL", "MATURE", "HYPE_OR_NOISE", "INSUFFICIENT_EVIDENCE", "OFF_TOPIC"
    ]
    decision_reason: str
    features: list[FeatureSchema]
    model_version_id: str
    predicted_stage: int | None = None
    predicted_trend: int | None = None
    enrichment_applied: bool


class HealthSchema(BaseModel):
    """Ответ `/healthz`."""

    status: Literal["ok"]


class ReadinessSchema(BaseModel):
    """Ответ `/readyz`."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, Any]
