"""DTO между транспортом, сценариями и провайдерами LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from insight.domain.entities import CandidateContext, EvidenceDocument
from insight.domain.values import CallStatus, Purpose


@dataclass(frozen=True, slots=True)
class GenerateInsightCommand:
    """Валидированный запрос `GenerateInsight`."""

    idempotency_key: str
    candidate: CandidateContext
    evidence: tuple[EvidenceDocument, ...]
    allow_fallback: bool = True
    max_output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Сообщение диалога для провайдера."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Ответ провайдера: только текст и расход токенов, без служебных полей SDK."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        """Суммарный расход токенов вызова."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class CallRecord:
    """Запись журнала вызовов LLM: метаданные без промптов и ответов (§14.5 ТЗ)."""

    purpose: Purpose
    provider: str
    model: str
    prompt_version: str
    request_sha256: str
    latency_ms: int
    status: CallStatus
    idempotency_key: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderState:
    """Состояние провайдера для `GetProviderStatus` и метрик."""

    provider: str
    enabled: bool
    healthy: bool
    max_concurrency: int
    in_flight: int = 0
    queued: int = 0
    last_error: str = ""
    last_success_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    """Зарегистрированная версия промпта (таблица `prompt_versions`)."""

    prompt_version: str
    purpose: Purpose
    template_sha256: str
    output_schema_version: str
    description: str = ""


@dataclass(slots=True)
class GenerationAttempt:
    """Состояние попытки генерации: накопленные замечания для повторного промпта."""

    number: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        """Замечания предыдущей попытки, которые передаются модели дословно."""
        return "; ".join(self.failures)
