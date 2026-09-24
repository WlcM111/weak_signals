"""Значения домена analyzer: перечисления, параметры анализа, статистика, аренда."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

DEFAULT_TOP_N = 15
DEFAULT_MAX_CANDIDATES = 40
DEFAULT_MIN_EVIDENCE_DOCUMENTS = 2
CONFIDENT_SCORE = 0.75


class OperationStatus(StrEnum):
    """Статус анализа (`common.v1.OperationStatus`; PARTIAL для анализа не применяется)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Терминальные статусы анализа."""
        return self in {OperationStatus.COMPLETED, OperationStatus.FAILED, OperationStatus.CANCELLED}


class Decision(StrEnum):
    """Решение по кандидату (`analyzer.v1.Decision`)."""

    WEAK_SIGNAL = "WEAK_SIGNAL"
    MATURE = "MATURE"
    HYPE_OR_NOISE = "HYPE_OR_NOISE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OFF_TOPIC = "OFF_TOPIC"


class DecisionReason(StrEnum):
    """Причина решения (`analyzer.v1.DecisionReason`, §12.6 ТЗ)."""

    MODEL_SCORE = "MODEL_SCORE"
    ENCYCLOPEDIA_MATURE = "ENCYCLOPEDIA_MATURE"
    MARKET_LEADERS = "MARKET_LEADERS"
    MATURITY_LEXICON = "MATURITY_LEXICON"
    MARKETING_DOMINANT = "MARKETING_DOMINANT"
    HYPE_LEXICON = "HYPE_LEXICON"
    NO_TRUSTED_SOURCE = "NO_TRUSTED_SOURCE"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    LOW_QUERY_RELEVANCE = "LOW_QUERY_RELEVANCE"


class FeatureDirection(StrEnum):
    """Направление вклада признака в решение (§12.8 ТЗ)."""

    SUPPORTS_WEAK_SIGNAL = "supports_weak_signal"
    SUPPORTS_MATURE = "supports_mature"
    NEUTRAL = "neutral"


class AnalysisErrorCode(StrEnum):
    """Коды отказов анализа (§14 HANDOFF)."""

    COLLECTOR_UNAVAILABLE = "COLLECTOR_UNAVAILABLE"
    NO_DOCUMENTS = "NO_DOCUMENTS"
    EMBEDDING_FAILED = "EMBEDDING_FAILED"
    MODEL_ERROR = "MODEL_ERROR"
    CANCELLED = "CANCELLED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SourceType(StrEnum):
    """Тип источника документа (`common.v1.SourceType`)."""

    SCIENTIFIC_PUBLICATION = "SCIENTIFIC_PUBLICATION"
    PREPRINT = "PREPRINT"
    PATENT = "PATENT"
    NEWS = "NEWS"
    INDUSTRY_MEDIA = "INDUSTRY_MEDIA"
    CORPORATE_BLOG = "CORPORATE_BLOG"
    PRESS_RELEASE = "PRESS_RELEASE"
    CODE_REPOSITORY = "CODE_REPOSITORY"
    VACANCY = "VACANCY"
    ANALYTICAL_REPORT = "ANALYTICAL_REPORT"
    GOVERNMENT = "GOVERNMENT"
    SOCIAL_MEDIA = "SOCIAL_MEDIA"
    ENCYCLOPEDIA = "ENCYCLOPEDIA"
    OTHER = "OTHER"


class TrustLevel(StrEnum):
    """Уровень доверенности источника (`common.v1.TrustLevel`)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Числовой ранг для сравнения (HIGH = 3)."""
        return {TrustLevel.HIGH: 3, TrustLevel.MEDIUM: 2, TrustLevel.LOW: 1}[self]


SCIENTIFIC_TYPES = frozenset({SourceType.SCIENTIFIC_PUBLICATION, SourceType.PREPRINT})
NEWS_TYPES = frozenset({SourceType.NEWS, SourceType.INDUSTRY_MEDIA})
MARKETING_TYPES = frozenset({SourceType.PRESS_RELEASE, SourceType.CORPORATE_BLOG})
CODE_VACANCY_TYPES = frozenset({SourceType.CODE_REPOSITORY, SourceType.VACANCY})


@dataclass(frozen=True, slots=True)
class AnalysisParams:
    """Параметры анализа (`analyzer.v1.AnalysisParams`); нулевые поля запроса → значения по умолчанию."""

    top_n: int
    max_candidates: int
    weak_signal_threshold: float
    min_evidence_documents: int

    RANGES = {  # noqa: RUF012 - неизменяемый справочник диапазонов из proto/DDL
        "top_n": (1, 50),
        "max_candidates": (5, 100),
        "min_evidence_documents": (1, 1000),
    }

    @staticmethod
    def from_request(
        model_threshold: float,
        top_n: int = 0,
        max_candidates: int = 0,
        weak_signal_threshold: float = 0.0,
        min_evidence_documents: int = 0,
    ) -> AnalysisParams:
        """Подставляет умолчания: 15 / 40 / порог активной модели / 2."""
        return AnalysisParams(
            top_n=top_n or DEFAULT_TOP_N,
            max_candidates=max_candidates or DEFAULT_MAX_CANDIDATES,
            weak_signal_threshold=weak_signal_threshold or model_threshold,
            min_evidence_documents=min_evidence_documents or DEFAULT_MIN_EVIDENCE_DOCUMENTS,
        )


@dataclass(frozen=True, slots=True)
class AnalysisStats:
    """Статистика анализа (`analyzer.v1.AnalysisStats`)."""

    documents_input: int = 0
    documents_after_dedup: int = 0
    clusters_total: int = 0
    candidates_scored: int = 0
    weak_signals_total: int = 0
    weak_signals_confident: int = 0
    excluded_mature: int = 0
    excluded_hype_or_noise: int = 0
    excluded_insufficient_evidence: int = 0
    excluded_off_topic: int = 0
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class Lease:
    """Аренда анализа воркером."""

    owner: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """Истекла ли аренда к моменту `now`."""
        return self.expires_at <= now
