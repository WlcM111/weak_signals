"""Сущности домена orchestrator: запрос, задание и неизменяемый снимок результата."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from orchestrator.domain.errors import InvariantViolation
from orchestrator.domain.values import (
    MAX_ATTEMPTS,
    MAX_QUERY_LENGTH,
    MAX_TOP_N,
    MIN_QUERY_LENGTH,
    MIN_TOP_N,
    ConfidenceBand,
    Decision,
    FeatureDirection,
    JobErrorCode,
    JobStats,
    JobStatus,
    Lease,
    NarrativeStatus,
    SummaryKind,
    TrustLevel,
    normalize_query,
)

MAX_TITLE_LENGTH = 200
MAX_ERROR_MESSAGE = 2000
MAX_SUMMARY_LENGTH = 400
KEY_PREDICTORS = 5

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # FAILED из очереди — отказ до начала работы, когда сквозной срок задания истёк в ожидании.
    JobStatus.QUEUED: frozenset({JobStatus.COLLECTING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.COLLECTING: frozenset(
        {JobStatus.ANALYZING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.ANALYZING: frozenset(
        {JobStatus.NARRATING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.NARRATING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.PARTIAL,
            JobStatus.CANCELLED,
            JobStatus.QUEUED,
            JobStatus.FAILED,
        }
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.PARTIAL: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Query:
    """Запрос пользователя (таблица `queries`)."""

    query_id: str
    text: str
    normalized_text: str
    requested_top_n: int
    client_ip_hash: str | None = None
    created_at: datetime | None = None

    @staticmethod
    def create(query_id: str, text: str, top_n: int, client_ip_hash: str | None = None) -> Query:
        """Создаёт запрос с нормализованным текстом и проверкой диапазонов."""
        cleaned = " ".join(text.split())
        if not MIN_QUERY_LENGTH <= len(cleaned) <= MAX_QUERY_LENGTH:
            raise InvariantViolation("query_text: 2..500 символов")
        if not MIN_TOP_N <= top_n <= MAX_TOP_N:
            raise InvariantViolation("top_n: 1..50")
        return Query(
            query_id=query_id,
            text=cleaned,
            normalized_text=normalize_query(cleaned),
            requested_top_n=top_n,
            client_ip_hash=client_ip_hash,
        )


@dataclass(slots=True)
class Job:
    """Задание: длительная операция из четырёх стадий (таблица `jobs`)."""

    job_id: str
    query_id: str
    status: JobStatus = JobStatus.QUEUED
    attempt: int = 0
    cancel_requested: bool = False
    lease: Lease | None = None
    collection_id: str = ""
    analysis_id: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    query_text: str = ""
    requested_top_n: int = 15

    def can_transition(self, target: JobStatus) -> bool:
        """Допустим ли переход в указанный статус."""
        return target in _ALLOWED_TRANSITIONS[self.status]

    def transition(self, target: JobStatus, at: datetime) -> None:
        """Переход статуса с проверкой таблицы переходов и инварианта `finished_at`."""
        if not self.can_transition(target):
            raise InvariantViolation(f"недопустимый переход {self.status} → {target}")
        self.status = target
        if target is JobStatus.COLLECTING and self.started_at is None:
            self.started_at = at
        if target.is_terminal:
            self.finished_at = at
            self.lease = None
        if target is JobStatus.QUEUED:
            self.lease = None

    def fail(self, code: str, message: str, at: datetime) -> None:
        """Терминальный отказ задания."""
        self.transition(JobStatus.FAILED, at)
        self.error_code = code
        self.error_message = message[:MAX_ERROR_MESSAGE]

    def cancel(self, at: datetime) -> None:
        """Отмена задания пользователем."""
        self.transition(JobStatus.CANCELLED, at)
        self.error_code = JobErrorCode.CANCELLED_BY_USER.value
        self.error_message = "задание отменено пользователем"

    def requeue(self, at: datetime) -> bool:
        """Возврат в очередь после потери аренды.

        Возвращает True, если попытка ещё осталась; при исчерпании `MAX_ATTEMPTS` задание
        завершается отказом `LEASE_EXPIRED_MAX_ATTEMPTS` и возвращается False.
        """
        if self.attempt + 1 > MAX_ATTEMPTS:
            self.fail(
                JobErrorCode.LEASE_EXPIRED_MAX_ATTEMPTS.value,
                f"аренда истекала {MAX_ATTEMPTS} раза подряд, задание прекращено",
                at,
            )
            return False
        self.attempt += 1
        self.transition(JobStatus.QUEUED, at)
        return True

    @property
    def is_terminal(self) -> bool:
        """Достигнут ли терминальный статус."""
        return self.status.is_terminal


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Признак элемента выдачи (таблица `result_item_features`)."""

    feature_name: str
    label_ru: str
    value: float
    contribution: float
    direction: FeatureDirection
    display_order: int

    def __post_init__(self) -> None:
        if self.display_order < 1:
            raise InvariantViolation("display_order ≥ 1")


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Источник элемента выдачи (таблица `result_item_sources`, требования ТЗ к источникам)."""

    position: int
    document_id: str
    title: str
    url: str
    source_type: str
    source_key: str
    language_code: str
    trust_level: TrustLevel
    summary_ru: str
    summary_kind: SummaryKind
    snippet: str
    similarity: float
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvariantViolation("position ≥ 1")
        if not 0.0 <= self.similarity <= 1.0:
            raise InvariantViolation("similarity: 0..1")
        if len(self.summary_ru) > MAX_SUMMARY_LENGTH:
            raise InvariantViolation("summary_ru: не более 400 символов")


@dataclass(slots=True)
class ResultItem:
    """Элемент итоговой выдачи — снимок кандидата с нарративом (таблица `result_items`)."""

    job_id: str
    rank: int
    candidate_id: str
    title_ru: str
    title_auto: str
    score: float
    decision_reason: str
    decision_explanation_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    explanation_ru: str
    narrative_status: NarrativeStatus
    llm_provider: str
    llm_model: str
    prompt_version: str
    document_count: int
    features: tuple[FeatureRow, ...] = ()
    sources: tuple[SourceRow, ...] = ()
    case_document_id: str = ""
    predicted_stage: int | None = None
    predicted_trend: int | None = None
    item_id: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise InvariantViolation("rank ≥ 1")
        if not 0.0 <= self.score <= 1.0:
            raise InvariantViolation("score: 0..1")
        if not 1 <= len(self.title_ru) <= MAX_TITLE_LENGTH:
            raise InvariantViolation("title_ru: 1..200 символов")
        if len(self.title_auto) > MAX_TITLE_LENGTH:
            raise InvariantViolation("title_auto: не более 200 символов")
        if self.predicted_stage is not None and not 1 <= self.predicted_stage <= 4:
            raise InvariantViolation("predicted_stage: 1..4")
        if self.predicted_trend is not None and not 1 <= self.predicted_trend <= 3:
            raise InvariantViolation("predicted_trend: 1..3")

    @property
    def confidence_band(self) -> ConfidenceBand:
        """Полоса уверенности для интерфейса."""
        return ConfidenceBand.of(self.score)

    @property
    def key_predictors(self) -> tuple[FeatureRow, ...]:
        """Ключевые предикторы: первые признаки по порядку отображения (§4 ТЗ)."""
        return tuple(sorted(self.features, key=lambda item: item.display_order)[:KEY_PREDICTORS])


@dataclass(frozen=True, slots=True)
class ExcludedCandidate:
    """Исключённый кандидат с причиной (таблица `excluded_candidates`)."""

    candidate_id: str
    title_auto: str
    score: float
    decision: Decision
    decision_reason: str
    decision_explanation_ru: str
    document_count: int

    def __post_init__(self) -> None:
        if self.decision is Decision.WEAK_SIGNAL:
            raise InvariantViolation("исключённым не может быть WEAK_SIGNAL")


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    """Снимок результата задания: элементы, исключённые кандидаты и статистика."""

    job: Job
    query_text: str
    items: tuple[ResultItem, ...] = ()
    excluded: tuple[ExcludedCandidate, ...] = ()
    stats: JobStats = field(default_factory=JobStats)

    def __post_init__(self) -> None:
        check_ranks(self.items)
        identifiers = [item.candidate_id for item in self.items]
        if len(set(identifiers)) != len(identifiers):
            raise InvariantViolation("candidate_id повторяется в снимке задания")


def check_ranks(items: Sequence[ResultItem]) -> None:
    """Ранги снимка обязаны быть 1..N без пропусков и повторов (§6 HANDOFF)."""
    ranks = sorted(item.rank for item in items)
    if ranks != list(range(1, len(ranks) + 1)):
        raise InvariantViolation(f"ранги снимка должны быть 1..{len(ranks)} без пропусков: {ranks}")
