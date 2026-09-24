"""Объекты передачи данных между транспортом, use cases и адаптерами analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from analyzer.domain.entities import Candidate, FeatureContribution, ModelVersion
from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.rules import RuleThresholds
from analyzer.domain.values import (
    AnalysisParams,
    AnalysisStats,
    Decision,
    DecisionReason,
    OperationStatus,
)


@dataclass(frozen=True, slots=True)
class AnalysisDraft:
    """Данные для вставки нового анализа."""

    idempotency_key: str
    collection_id: str
    query_text: str
    model_version_id: str
    params: AnalysisParams


@dataclass(frozen=True, slots=True)
class StartAnalysisCommand:
    """Валидированный запрос запуска анализа."""

    idempotency_key: str
    collection_id: str
    query_text: str
    params_raw: tuple[int, int, float, int]


@dataclass(frozen=True, slots=True)
class StartAnalysisResult:
    """Результат запуска анализа (`StartAnalysisResponse`)."""

    analysis_id: str
    status: OperationStatus
    already_existed: bool
    model_version_id: str


@dataclass(frozen=True, slots=True)
class AnalysisView:
    """Состояние анализа для `GetAnalysis`."""

    analysis_id: str
    status: OperationStatus
    model_version_id: str
    stats: AnalysisStats
    error_code: str
    error_message: str
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidatePage:
    """Страница кандидатов для `ListCandidates`."""

    candidates: tuple[Candidate, ...]
    next_page_token: str
    total_count: int


@dataclass(frozen=True, slots=True)
class ScoreTextResult:
    """Результат прямого скоринга описания технологии."""

    score: float
    decision: Decision
    decision_reason: DecisionReason
    features: tuple[FeatureContribution, ...]
    model_version_id: str
    enrichment_applied: bool
    explanation_ru: str = ""
    predicted_stage: int | None = None
    predicted_trend: int | None = None


@dataclass(frozen=True, slots=True)
class EncyclopediaHit:
    """Ответ `collector.CheckEncyclopedia` по одному названию."""

    title: str
    exists: bool
    page_url: str = ""
    pageviews_30d: int = -1
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CollectionInfo:
    """Сведения о коллекции collector-а, нужные для проверки предусловий."""

    collection_id: str
    status: str
    documents_total: int
    is_terminal: bool


@dataclass(frozen=True, slots=True)
class LeaseState:
    """Состояние аренды после heartbeat."""

    alive: bool
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class CachedEmbedding:
    """Запись кеша эмбеддингов документа."""

    document_id: str
    content_hash: str
    vector: np.ndarray


@dataclass(frozen=True, slots=True)
class ModelBundle:
    """Загруженная активная модель: метаданные, артефакты и производные настройки."""

    version: ModelVersion
    classifier: Any
    registry: FeatureRegistry
    thresholds: RuleThresholds
    weak_centroid: np.ndarray | None = None
    mature_centroid: np.ndarray | None = None
    feature_defaults: dict[str, float] = field(default_factory=dict)
    # v2: признаки темы, кандидата и свидетельств; спецификация (проекция, глоссарий) — из артефакта.
    feature_schema: str = "v1"
    v2: Any = None

    @property
    def threshold(self) -> float:
        """Порог решения активной модели."""
        return self.version.threshold
