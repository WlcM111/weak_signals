"""Значения домена orchestrator: статусы, коды отказов, аренда, статистика, прогресс."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_ATTEMPTS = 3
MAX_POSTPONEMENTS = 10
CONFIDENT_SCORE = 0.75
DEFAULT_TOP_N = 15
MIN_TOP_N = 1
MAX_TOP_N = 50
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 500


class JobStatus(StrEnum):
    """Статус задания (`orchestrator.openapi.yaml`, `JobStatus`)."""

    QUEUED = "QUEUED"
    COLLECTING = "COLLECTING"
    ANALYZING = "ANALYZING"
    NARRATING = "NARRATING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Терминальные статусы задания."""
        return self in {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def is_running(self) -> bool:
        """Статусы, при которых задание удерживает аренду."""
        return self in {JobStatus.COLLECTING, JobStatus.ANALYZING, JobStatus.NARRATING}


class JobErrorCode(StrEnum):
    """Коды отказов задания (§7 COMMON, §7 HANDOFF)."""

    TOO_FEW_DOCUMENTS = "TOO_FEW_DOCUMENTS"
    COLLECTION_FAILED = "COLLECTION_FAILED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    LEASE_EXPIRED_MAX_ATTEMPTS = "LEASE_EXPIRED_MAX_ATTEMPTS"
    POSTPONED_MAX_ATTEMPTS = "POSTPONED_MAX_ATTEMPTS"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class Decision(StrEnum):
    """Решение analyzer по кандидату (`analyzer.v1.Decision`)."""

    WEAK_SIGNAL = "WEAK_SIGNAL"
    MATURE = "MATURE"
    HYPE_OR_NOISE = "HYPE_OR_NOISE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OFF_TOPIC = "OFF_TOPIC"


class NarrativeStatus(StrEnum):
    """Происхождение нарратива (`insight.v1.InsightStatus`)."""

    GENERATED = "GENERATED"
    FALLBACK_EXTRACTIVE = "FALLBACK_EXTRACTIVE"


class SummaryKind(StrEnum):
    """Вид русскоязычного резюме источника (требование ТЗ об отметке перевода)."""

    ORIGINAL_RU = "ORIGINAL_RU"
    GENERATIVE_SUMMARY = "GENERATIVE_SUMMARY"
    EXTRACTIVE = "EXTRACTIVE"


class FeatureDirection(StrEnum):
    """Направление вклада признака."""

    SUPPORTS_WEAK_SIGNAL = "supports_weak_signal"
    SUPPORTS_MATURE = "supports_mature"
    NEUTRAL = "neutral"


class TrustLevel(StrEnum):
    """Уровень доверенности источника."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ConfidenceBand(StrEnum):
    """Полоса уверенности для интерфейса (`ResultItemSummary.confidence_band`)."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"

    @staticmethod
    def of(score: float) -> ConfidenceBand:
        """Полоса по значению скоринга: ≥ 0.75 High, ≥ 0.5 Medium, иначе Low."""
        if score >= CONFIDENT_SCORE:
            return ConfidenceBand.HIGH
        if score >= 0.5:
            return ConfidenceBand.MEDIUM
        return ConfidenceBand.LOW


@dataclass(frozen=True, slots=True)
class Lease:
    """Аренда задания воркером."""

    owner: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """Истекла ли аренда."""
        return self.expires_at <= now


@dataclass(frozen=True, slots=True)
class JobStats:
    """Статистика задания (таблица `job_stats`, схема `ResultStats`)."""

    http_requests_total: int = 0
    sources_processed: int = 0
    documents_collected: int = 0
    candidates_found: int = 0
    weak_signals_total: int = 0
    weak_signals_confident: int = 0
    collect_ms: int | None = None
    analyze_ms: int | None = None
    narrate_ms: int | None = None
    narratives_generated: int = 0
    narratives_fallback: int = 0
    model_version_id: str = ""
    expand_used_fallback: bool | None = None


@dataclass(frozen=True, slots=True)
class JobProgress:
    """Прогресс задания для `GET /api/v1/jobs/{job_id}` (схема `JobProgress`)."""

    stage: JobStatus
    http_requests_total: int = 0
    sources_processed: int = 0
    documents_collected: int = 0
    candidates_found: int = 0
    narratives_done: int = 0
    narratives_total: int = 0


def normalize_query(text: str) -> str:
    """Нормализация текста запроса: нижний регистр, обрезка, схлопывание пробелов (§6 HANDOFF)."""
    return " ".join(text.lower().split())
