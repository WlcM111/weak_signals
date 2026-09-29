"""Дублёры портов insight: часы, репозитории в памяти и готовые доменные объекты."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from insight.application.dto import CallRecord, PromptDefinition
from insight.domain.entities import (
    CandidateContext,
    EvidenceDocument,
    FeatureContribution,
    Insight,
    QueryExpansion,
)
from insight.domain.values import Decision, FeatureDirection, TrustLevel

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def new_id(seed: int) -> str:
    """Детерминированный UUID для тестов."""
    return str(uuid.UUID(int=seed, version=4))


class FakeClock:
    """Управляемые часы: `sleep` не ждёт, а двигает время."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start
        self._monotonic = 0.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        """Текущее «время»."""
        return self._now

    def monotonic(self) -> float:
        """Монотонный счётчик секунд."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Пауза сдвигает часы вперёд."""
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Сдвигает календарное и монотонное время."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


@dataclass
class InMemoryInsightRepository:
    """Реализация `InsightRepository` в памяти."""

    by_key: dict[str, Insight] = field(default_factory=dict)
    saves: int = 0

    async def get_by_idempotency_key(self, key: str) -> Insight | None:
        """Инсайт по ключу идемпотентности."""
        return self.by_key.get(key)

    async def get_by_input_hash(self, input_hash: str, not_older_than: datetime) -> Insight | None:
        """Свежий инсайт с тем же входом."""
        for insight in self.by_key.values():
            created = insight.created_at or NOW
            if insight.input_hash == input_hash and created >= not_older_than:
                return insight
        return None

    async def save(self, insight: Insight) -> Insight:
        """Сохраняет инсайт, присваивая идентификатор."""
        self.saves += 1
        stored = Insight(
            idempotency_key=insight.idempotency_key,
            input_hash=insight.input_hash,
            candidate_id=insight.candidate_id,
            prompt_version=insight.prompt_version,
            status=insight.status,
            narrative=insight.narrative,
            grounding=insight.grounding,
            provenance=insight.provenance,
            summaries=insight.summaries,
            insight_id=new_id(900 + self.saves),
            attempts=insight.attempts,
            created_at=NOW,
        )
        self.by_key[insight.idempotency_key] = stored
        return stored


@dataclass
class InMemoryExpansionRepository:
    """Реализация `ExpansionRepository` в памяти."""

    items: dict[str, QueryExpansion] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    async def get(self, query_norm: str, prompt_version: str | None = None) -> QueryExpansion | None:
        """Сохранённое удачное расширение той же версии промпта (как в Postgres)."""
        found = self.items.get(query_norm)
        if found is None or found.used_fallback:
            return None
        if prompt_version is not None and self.versions.get(query_norm, prompt_version) != prompt_version:
            return None
        return found

    async def save(self, expansion: QueryExpansion, prompt_version: str) -> None:
        """Сохраняет расширение."""
        self.items[expansion.query_norm] = expansion
        self.versions[expansion.query_norm] = prompt_version


@dataclass
class InMemoryCallLog:
    """Реализация `LLMCallLog` в памяти; помогает проверить отсутствие текстов в журнале."""

    records: list[CallRecord] = field(default_factory=list)
    tokens_today: int = 0

    async def record(self, entry: CallRecord) -> None:
        """Пишет запись журнала."""
        self.records.append(entry)

    async def tokens_used_today(self, now: datetime) -> int:
        """Сумма токенов за сутки."""
        return self.tokens_today


@dataclass
class InMemoryPromptRegistry:
    """Реализация `PromptRegistry` в памяти."""

    definitions: list[PromptDefinition] = field(default_factory=list)

    async def register(self, definitions: Sequence[PromptDefinition]) -> None:
        """Регистрирует версии промптов."""
        self.definitions = list(definitions)


def make_evidence(index: int, language: str = "en", trust: TrustLevel = TrustLevel.HIGH) -> EvidenceDocument:
    """Доказательный документ для тестов."""
    return EvidenceDocument(
        document_id=new_id(100 + index),
        title=f"Research paper {index}",
        url=f"https://example.org/{index}",
        text=(
            f"Researchers demonstrated a prototype in {index} laboratories. "
            "The pilot covered 37 devices and reduced energy use by 45 percent. "
            "A seed round of 12 million dollars was closed."
        ),
        language_code=language,
        source_type="SCIENTIFIC_PUBLICATION",
        trust_level=trust,
        published_at=NOW,
    )


def make_candidate(features: int = 2) -> CandidateContext:
    """Кандидат analyzer для тестов."""
    labels = [
        ("lex_emergence_score", "Лексика ранней стадии", 0.82, 0.41, FeatureDirection.SUPPORTS_WEAK_SIGNAL),
        ("trusted_share", "Доля доверенных источников", 0.60, 0.15, FeatureDirection.SUPPORTS_WEAK_SIGNAL),
        ("lex_maturity_score", "Лексика зрелости рынка", 0.10, -0.22, FeatureDirection.SUPPORTS_MATURE),
    ]
    return CandidateContext(
        candidate_id=new_id(200),
        title="Нейроморфные чипы для edge-инференса",
        keyphrases=("нейроморфные чипы", "edge-инференс"),
        score=0.88,
        decision=Decision.WEAK_SIGNAL,
        query_text="слабые сигналы в кибербезопасности",
        top_features=tuple(
            FeatureContribution(name, label, value, contribution, direction)
            for name, label, value, contribution, direction in labels[:features]
        ),
    )
