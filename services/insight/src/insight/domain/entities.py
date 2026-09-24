"""Сущности домена insight: кандидат с доказательствами, нарратив, инсайт, расширение запроса."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from insight.domain.errors import InvariantViolation
from insight.domain.values import (
    MAX_ADVANTAGE,
    MAX_CASE_EXAMPLE,
    MAX_DESCRIPTION,
    MAX_EXPLANATION,
    MAX_SUMMARY,
    MAX_TITLE,
    Decision,
    FeatureDirection,
    InsightStatus,
    Provenance,
    SummaryKind,
    TrustLevel,
)


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    """Признак кандидата, переданный analyzer (`analyzer.v1.FeatureContribution`)."""

    feature_name: str
    label_ru: str
    value: float
    contribution: float
    direction: FeatureDirection


@dataclass(frozen=True, slots=True)
class EvidenceDocument:
    """Доказательный документ кандидата; `text` уже усечён вызывающей стороной."""

    document_id: str
    title: str
    url: str
    text: str
    language_code: str
    source_type: str
    trust_level: TrustLevel
    published_at: datetime | None = None

    @property
    def is_russian(self) -> bool:
        """Русскоязычный ли источник (резюме для него не требует генерации)."""
        return self.language_code.lower().startswith("ru")

    @property
    def origin_domain(self) -> str:
        """Домен источника для экстрактивного кейс-примера."""
        without_scheme = self.url.split("://", 1)[-1]
        return without_scheme.split("/", 1)[0]


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """Кандидат, для которого строится нарратив."""

    candidate_id: str
    title: str
    keyphrases: tuple[str, ...]
    score: float
    decision: Decision
    query_text: str
    top_features: tuple[FeatureContribution, ...] = ()


@dataclass(frozen=True, slots=True)
class Narrative:
    """Русскоязычный нарратив кандидата (`insight.v1.Narrative`)."""

    title_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    explanation_ru: str
    case_document_id: str = ""

    def __post_init__(self) -> None:
        limits = (
            ("title_ru", self.title_ru, MAX_TITLE),
            ("description_ru", self.description_ru, MAX_DESCRIPTION),
            ("advantage_ru", self.advantage_ru, MAX_ADVANTAGE),
            ("case_example_ru", self.case_example_ru, MAX_CASE_EXAMPLE),
            ("explanation_ru", self.explanation_ru, MAX_EXPLANATION),
        )
        for name, value, limit in limits:
            if len(value) > limit:
                raise InvariantViolation(f"Narrative.{name}: не более {limit} символов")
        if not self.title_ru.strip():
            raise InvariantViolation("Narrative.title_ru: пустое название")


@dataclass(frozen=True, slots=True)
class SourceSummary:
    """Русскоязычное резюме источника с отметкой о происхождении (требование ТЗ)."""

    document_id: str
    summary_ru: str
    kind: SummaryKind
    position: int = 1

    def __post_init__(self) -> None:
        if len(self.summary_ru) > MAX_SUMMARY:
            raise InvariantViolation("SourceSummary.summary_ru: не более 400 символов")
        if self.position < 1:
            raise InvariantViolation("SourceSummary.position ≥ 1")


@dataclass(frozen=True, slots=True)
class GroundingCheck:
    """Итог проверки обоснованности нарратива доказательствами (`insight.v1.GroundingCheck`)."""

    passed: bool
    unsupported_numbers: int = 0
    unknown_document_refs: int = 0
    features_mentioned: int = 0
    hard_failures: tuple[str, ...] = ()
    soft_failures: tuple[str, ...] = ()

    @property
    def has_hard_failure(self) -> bool:
        """Жёсткий провал: выдуманные числа, чужой документ или ссылка в никуда."""
        return bool(self.hard_failures)

    @property
    def summary(self) -> str:
        """Однострочное перечисление нарушений для повторного промпта и журнала."""
        return "; ".join((*self.hard_failures, *self.soft_failures))


@dataclass(frozen=True, slots=True)
class Insight:
    """Готовый инсайт: неизменяемая запись результата генерации (таблица `insights`)."""

    idempotency_key: str
    input_hash: str
    candidate_id: str
    prompt_version: str
    status: InsightStatus
    narrative: Narrative
    grounding: GroundingCheck
    provenance: Provenance
    summaries: tuple[SourceSummary, ...] = ()
    insight_id: str = ""
    attempts: int = 0
    from_cache: bool = False
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        positions = [summary.position for summary in self.summaries]
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise InvariantViolation("позиции резюме источников должны быть 1..N без пропусков")
        identifiers = [summary.document_id for summary in self.summaries]
        if len(set(identifiers)) != len(identifiers):
            raise InvariantViolation("резюме источников дублируются по document_id")


@dataclass(frozen=True, slots=True)
class QueryExpansion:
    """Расширение запроса в поисковые термины (таблица `query_expansions`)."""

    query_norm: str
    ru_terms: tuple[str, ...]
    en_terms: tuple[str, ...]
    domain_tags: tuple[str, ...] = ()
    used_fallback: bool = False
    provenance: Provenance = field(default_factory=Provenance)

    def __post_init__(self) -> None:
        if not self.ru_terms or not self.en_terms:
            raise InvariantViolation("расширение обязано содержать хотя бы один термин каждого языка")
