"""Значения домена collector: перечисления, поисковые фразы, лимиты, аренда.

Перечисления — `StrEnum` без префикса (в БД хранятся строками, в proto имеют префикс, §9 COMMON).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from collector.domain.errors import InvariantViolation


class SourceKey(StrEnum):
    """Ключ адаптера-источника (= `collector.source_catalog.source_key`, enum `SourceKey` в common.proto)."""

    OPENALEX = "openalex"
    ARXIV = "arxiv"
    PATENTSVIEW = "patentsview"
    RSS = "rss"
    GITHUB = "github"
    HH = "hh"
    WIKIPEDIA = "wikipedia"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    ZENODO = "zenodo"
    GDELT = "gdelt"


class SourceType(StrEnum):
    """Тип источника документа (enum `SourceType` в common.proto)."""

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
    """Уровень доверенности источника (enum `TrustLevel` в common.proto)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Числовой ранг для сравнения уровней (HIGH = 3)."""
        return {TrustLevel.HIGH: 3, TrustLevel.MEDIUM: 2, TrustLevel.LOW: 1}[self]

    def capped_at(self, ceiling: TrustLevel) -> TrustLevel:
        """Понижает уровень до потолка (документ без даты публикации — не выше MEDIUM, §6.5)."""
        return self if self.rank <= ceiling.rank else ceiling


class CollectionMode(StrEnum):
    """Режим сбора: полный по открытому запросу или короткий по названию технологии."""

    SEARCH = "SEARCH"
    ENRICHMENT = "ENRICHMENT"


class OperationStatus(StrEnum):
    """Статус длительной операции (enum `OperationStatus` в common.proto)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Терминальные статусы: документы коллекции доступны только в них (§9.2)."""
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {OperationStatus.COMPLETED, OperationStatus.PARTIAL, OperationStatus.FAILED, OperationStatus.CANCELLED}
)


class AdapterErrorCode(StrEnum):
    """Коды отказов адаптеров (§10.7 ТЗ)."""

    RATE_LIMITED = "RATE_LIMITED"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    TIMEOUT = "TIMEOUT"
    PARSE_ERROR = "PARSE_ERROR"
    AUTH_MISSING = "AUTH_MISSING"
    DISABLED = "DISABLED"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class CollectionErrorCode(StrEnum):
    """Коды отказов коллекции, не совпадающие с кодом адаптера (§14 HANDOFF)."""

    NO_DOCUMENTS = "NO_DOCUMENTS"
    CANCELLED = "CANCELLED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


MAX_TERMS_PER_LANGUAGE = 8
MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 120


@dataclass(frozen=True, slots=True)
class SearchTerms:
    """Поисковые фразы на русском и английском (ru + en ≥ 1, §10.1)."""

    ru: tuple[str, ...] = ()
    en: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.ru and not self.en:
            raise InvariantViolation("SearchTerms: требуется хотя бы одна фраза")

    def pairs(self) -> tuple[tuple[str, str], ...]:
        """Фразы в порядке обхода адаптерами: (фраза, код языка)."""
        return tuple((term, "ru") for term in self.ru) + tuple((term, "en") for term in self.en)

    @property
    def total(self) -> int:
        """Общее число фраз."""
        return len(self.ru) + len(self.en)


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    """Лимиты сбора; нулевое поле означает «значение по умолчанию режима» (proto CollectionLimits)."""

    max_documents_per_source: int
    max_total_documents: int
    time_budget_seconds: int
    published_since_year: int

    RANGES = {  # noqa: RUF012 - неизменяемый справочник диапазонов из proto/DDL
        "max_documents_per_source": (1, 500),
        "max_total_documents": (1, 3000),
        "time_budget_seconds": (10, 600),
        "published_since_year": (2000, 2100),
    }

    @staticmethod
    def defaults(mode: CollectionMode) -> CollectionLimits:
        """Значения по умолчанию по режиму (§6 HANDOFF: SEARCH 150/800/120/2023, ENRICHMENT 40/200/45/2023)."""
        if mode is CollectionMode.SEARCH:
            return CollectionLimits(150, 800, 120, 2023)
        return CollectionLimits(40, 200, 45, 2023)

    @staticmethod
    def from_request(
        mode: CollectionMode,
        max_documents_per_source: int = 0,
        max_total_documents: int = 0,
        time_budget_seconds: int = 0,
        published_since_year: int = 0,
    ) -> CollectionLimits:
        """Подставляет значения по умолчанию вместо нулевых полей запроса."""
        defaults = CollectionLimits.defaults(mode)
        return CollectionLimits(
            max_documents_per_source or defaults.max_documents_per_source,
            max_total_documents or defaults.max_total_documents,
            time_budget_seconds or defaults.time_budget_seconds,
            published_since_year or defaults.published_since_year,
        )


@dataclass(frozen=True, slots=True)
class Lease:
    """Аренда коллекции воркером: владелец и момент истечения."""

    owner: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """Истекла ли аренда к моменту `now`."""
        return self.expires_at <= now
