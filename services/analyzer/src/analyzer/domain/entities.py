"""Сущности домена analyzer: анализ, кандидат, документ в памяти, версия модели."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from analyzer.domain.errors import InvariantViolation
from analyzer.domain.values import (
    AnalysisParams,
    AnalysisStats,
    Decision,
    DecisionReason,
    FeatureDirection,
    Lease,
    OperationStatus,
    SourceType,
    TrustLevel,
)

MAX_TITLE_LENGTH = 200
MAX_EXPLANATION_LENGTH = 500
MAX_SNIPPET_LENGTH = 600
MAX_KEYPHRASES = 10

_ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PENDING: frozenset({OperationStatus.RUNNING, OperationStatus.CANCELLED}),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.PENDING,  # возврат в очередь при потере аренды
        }
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """Документ коллекции, загруженный в память анализа (подмножество `common.v1.Document`)."""

    document_id: str
    title: str
    text: str
    language_code: str
    source_type: SourceType
    trust_level: TrustLevel
    origin_domain: str
    url: str = ""
    published_at: datetime | None = None
    citation_count: int | None = None
    engagement_count: int | None = None
    content_hash: str = ""
    relevance_rank: int = 0

    @property
    def embedding_text(self) -> str:
        """Текст для эмбеддинга: `title + ". " + text[:1500]` (§12.5 ТЗ)."""
        body = self.text[:1500].strip()
        return f"{self.title}. {body}".strip() if body else self.title

    @property
    def published_year(self) -> int | None:
        """Год публикации, если дата известна."""
        return self.published_at.year if self.published_at else None

    def content_key(self) -> str:
        """Ключ инвалидации кеша эмбеддингов.

        Контракт `common.v1.Document` не передаёт хеш содержимого, поэтому он вычисляется по тому же
        тексту, который подаётся в эмбеддер: изменение текста документа делает кеш недействительным.
        """
        if self.content_hash:
            return self.content_hash
        return hashlib.sha256(self.embedding_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    """Вклад признака в решение (`analyzer.v1.FeatureContribution`)."""

    feature_name: str
    value: float
    contribution: float
    label_ru: str
    direction: FeatureDirection


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Доказательный документ кандидата (`analyzer.v1.Evidence`)."""

    document_id: str
    snippet: str
    similarity: float
    source_type: SourceType
    trust_level: TrustLevel
    published_year: int | None = None

    def __post_init__(self) -> None:
        if len(self.snippet) > MAX_SNIPPET_LENGTH:
            raise InvariantViolation("Evidence.snippet: не более 600 символов")
        if not 0.0 <= self.similarity <= 1.0:
            raise InvariantViolation("Evidence.similarity: 0..1")


@dataclass(frozen=True, slots=True)
class ClusterDocument:
    """Документ кластера с близостью к центроиду (строка `candidate_documents`)."""

    document_id: str
    similarity: float
    is_evidence: bool
    snippet: str
    source_type: SourceType
    trust_level: TrustLevel
    published_year: int | None = None


@dataclass(slots=True)
class Candidate:
    """Кандидат-технология: кластер документов с решением, признаками и доказательствами."""

    cluster_index: int
    title_auto: str
    keyphrases: tuple[str, ...]
    score: float
    decision: Decision
    decision_reason: DecisionReason
    decision_explanation_ru: str
    features: tuple[FeatureContribution, ...]
    documents: tuple[ClusterDocument, ...]
    document_count: int
    query_relevance: float
    source_type_counts: dict[SourceType, int] = field(default_factory=dict)
    year_counts: dict[int, int] = field(default_factory=dict)
    rank: int = 0
    candidate_id: str = ""
    predicted_stage: int | None = None
    predicted_trend: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.title_auto) <= MAX_TITLE_LENGTH:
            raise InvariantViolation("Candidate.title_auto: длина 1..200")
        if not 1 <= len(self.keyphrases) <= MAX_KEYPHRASES:
            raise InvariantViolation("Candidate.keyphrases: от 1 до 10 фраз")
        if not 0.0 <= self.score <= 1.0:
            raise InvariantViolation("Candidate.score: 0..1")
        if len(self.decision_explanation_ru) > MAX_EXPLANATION_LENGTH:
            raise InvariantViolation("Candidate.decision_explanation_ru: не более 500 символов")
        if self.document_count < 1:
            raise InvariantViolation("Candidate.document_count ≥ 1")
        if not 0.0 <= self.query_relevance <= 1.0:
            raise InvariantViolation("Candidate.query_relevance: 0..1")
        self._check_rank_invariant()

    def _check_rank_invariant(self) -> None:
        """`decision == WEAK_SIGNAL ⇔ rank ≥ 1` (CHECK таблицы `candidates`)."""
        is_weak = self.decision is Decision.WEAK_SIGNAL
        if is_weak and self.rank < 1:
            raise InvariantViolation("Candidate: WEAK_SIGNAL требует rank ≥ 1")
        if not is_weak and self.rank != 0:
            raise InvariantViolation("Candidate: исключённый кандидат должен иметь rank = 0")

    def with_rank(self, rank: int) -> Candidate:
        """Проставляет ранг с повторной проверкой инварианта."""
        self.rank = rank
        self._check_rank_invariant()
        return self

    @property
    def evidence(self) -> tuple[EvidenceItem, ...]:
        """Доказательства кандидата в порядке убывания близости."""
        return tuple(
            EvidenceItem(
                document_id=document.document_id,
                snippet=document.snippet,
                similarity=document.similarity,
                source_type=document.source_type,
                trust_level=document.trust_level,
                published_year=document.published_year,
            )
            for document in self.documents
            if document.is_evidence
        )

    def feature_value(self, name: str) -> float:
        """Значение признака по имени (0.0, если признак отсутствует)."""
        for feature in self.features:
            if feature.feature_name == name:
                return feature.value
        return 0.0


@dataclass(frozen=True, slots=True)
class ModelMetrics:
    """Метрики активной модели (`analyzer.v1.ModelMetrics`)."""

    accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: float
    threshold: float
    test_size: int
    evaluation_protocol: str


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """Версия модели из манифеста (`model_manifest.schema.json` + таблица `model_versions`)."""

    model_version_id: str
    model_family: str
    feature_schema_version: str
    embedding_model: str
    dataset_version: str
    artifact_path: str
    artifact_sha256: str
    trained_at: datetime
    metrics: ModelMetrics
    git_commit: str = ""
    stage_model_present: bool = False
    trend_model_present: bool = False
    is_active: bool = True

    @property
    def threshold(self) -> float:
        """Порог решения активной модели."""
        return self.metrics.threshold


@dataclass(slots=True)
class Analysis:
    """Анализ коллекции одной версией модели (таблица `analyses`)."""

    analysis_id: str
    idempotency_key: str
    collection_id: str
    query_text: str
    model_version_id: str
    params: AnalysisParams
    status: OperationStatus = OperationStatus.PENDING
    cancel_requested: bool = False
    lease: Lease | None = None
    stats: AnalysisStats = field(default_factory=AnalysisStats)
    error_code: str = ""
    error_message: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def can_transition_to(self, status: OperationStatus) -> bool:
        """Допустим ли переход в указанный статус."""
        return status in _ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, status: OperationStatus, at: datetime) -> None:
        """Переход статуса с проверкой матрицы переходов."""
        if not self.can_transition_to(status):
            raise InvariantViolation(f"недопустимый переход {self.status} → {status}")
        self.status = status
        if status is OperationStatus.RUNNING and self.started_at is None:
            self.started_at = at
        if status.is_terminal:
            self.finished_at = at
            self.lease = None

    def finish(
        self,
        status: OperationStatus,
        at: datetime,
        stats: AnalysisStats | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """Терминальное завершение анализа."""
        if not status.is_terminal:
            raise InvariantViolation("finish требует терминальный статус")
        self.transition_to(status, at)
        if stats is not None:
            self.stats = stats
        self.error_code = error_code
        self.error_message = error_message[:500]

    @property
    def is_terminal(self) -> bool:
        """Достигнут ли терминальный статус."""
        return self.status.is_terminal
