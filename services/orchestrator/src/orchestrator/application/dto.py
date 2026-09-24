"""DTO между HTTP-слоем, сценариями и клиентами сервисов."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from orchestrator.domain.entities import ExcludedCandidate, Job, ResultItem
from orchestrator.domain.values import Decision, FeatureDirection, JobProgress, JobStats, TrustLevel


@dataclass(frozen=True, slots=True)
class SubmitQueryCommand:
    """Валидированный запрос `POST /api/v1/queries`."""

    query_text: str
    top_n: int
    idempotency_key: str
    request_hash: str
    client_ip_hash: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitQueryResult:
    """Ответ `JobAccepted`."""

    job_id: str
    query_id: str
    status: str
    created: bool


@dataclass(frozen=True, slots=True)
class JobView:
    """Задание с прогрессом для `GET /api/v1/jobs/{job_id}`."""

    job: Job
    progress: JobProgress


@dataclass(frozen=True, slots=True)
class JobPage:
    """Страница списка заданий (keyset по `created_at desc, job_id`)."""

    items: tuple[JobView, ...]
    next_cursor: str = ""


@dataclass(frozen=True, slots=True)
class ResultsView:
    """Снимок результата для `GET /api/v1/jobs/{job_id}/results`."""

    job: Job
    query_text: str
    items: tuple[ResultItem, ...]
    excluded: tuple[ExcludedCandidate, ...]
    stats: JobStats


@dataclass(frozen=True, slots=True)
class CollectionView:
    """Состояние коллекции collector-а."""

    collection_id: str
    status: str
    documents_total: int
    http_requests_total: int = 0
    adapters_completed: int = 0
    failed_adapters: tuple[str, ...] = ()
    is_terminal: bool = False


@dataclass(frozen=True, slots=True)
class AnalysisView:
    """Состояние анализа analyzer-а."""

    analysis_id: str
    status: str
    model_version_id: str = ""
    candidates_scored: int = 0
    weak_signals_total: int = 0
    weak_signals_confident: int = 0
    error_code: str = ""
    error_message: str = ""
    is_terminal: bool = False


@dataclass(frozen=True, slots=True)
class FeatureView:
    """Вклад признака, полученный от analyzer."""

    feature_name: str
    label_ru: str
    value: float
    contribution: float
    direction: FeatureDirection


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """Доказательный документ кандидата."""

    document_id: str
    snippet: str
    similarity: float
    source_type: str
    trust_level: TrustLevel


@dataclass(frozen=True, slots=True)
class JudgeVerdictView:
    """Смысловая оценка кандидата моделью (`insight.v1.JudgeVerdict`)."""

    candidate_id: str
    verdict: str
    relevance: int
    reason_ru: str


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Кандидат, полученный из `ListCandidates`."""

    candidate_id: str
    rank: int
    title_auto: str
    keyphrases: tuple[str, ...]
    score: float
    decision: Decision
    decision_reason: str
    decision_explanation_ru: str
    document_count: int
    features: tuple[FeatureView, ...] = ()
    evidence: tuple[EvidenceView, ...] = ()
    predicted_stage: int | None = None
    predicted_trend: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentView:
    """Документ collector-а, нужный для доказательств и источников."""

    document_id: str
    title: str
    url: str
    text: str
    language_code: str
    source_type: str
    source_key: str
    trust_level: TrustLevel
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExpansionView:
    """Результат `ExpandQuery`."""

    ru_terms: tuple[str, ...]
    en_terms: tuple[str, ...]
    domain_tags: tuple[str, ...] = ()
    used_fallback: bool = False


@dataclass(frozen=True, slots=True)
class NarrativeView:
    """Результат `GenerateInsight`."""

    title_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    explanation_ru: str
    status: str
    llm_provider: str
    llm_model: str
    prompt_version: str
    case_document_id: str = ""
    source_summaries: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelInfoView:
    """Сведения об активной модели (прокси `AnalyzerService.GetModelInfo`)."""

    model_version_id: str
    model_family: str
    feature_schema_version: str
    embedding_model: str
    dataset_version: str
    trained_at: datetime
    metrics: dict[str, float]
    feature_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreTextView:
    """Результат прокси `AnalyzerService.ScoreText`."""

    score: float
    decision: Decision
    decision_reason: str
    features: tuple[FeatureView, ...]
    model_version_id: str
    enrichment_applied: bool
    predicted_stage: int | None = None
    predicted_trend: int | None = None
