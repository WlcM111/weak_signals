"""Значения домена insight: статусы, назначения вызовов, провайдеры, лимиты полей."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MAX_TITLE = 200
MAX_DESCRIPTION = 800
MAX_ADVANTAGE = 600
MAX_CASE_EXAMPLE = 600
MAX_EXPLANATION = 800
MAX_SUMMARY = 400
MAX_TERMS = 8
MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 120
MAX_DOMAIN_TAGS = 5
MAX_EVIDENCE = 8
MAX_FEATURES = 8
MAX_EVIDENCE_TEXT = 2000
MAX_IDEMPOTENCY_KEY = 160
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 500

DOMAIN_TAGS = (
    "industrial_ai",
    "robotics",
    "ai_infrastructure",
    "fintech",
    "ai_security",
    "edge",
    "other",
)


class InsightStatus(StrEnum):
    """Происхождение нарратива (`insight.v1.InsightStatus`)."""

    GENERATED = "GENERATED"
    FALLBACK_EXTRACTIVE = "FALLBACK_EXTRACTIVE"


class SummaryKind(StrEnum):
    """Вид русскоязычного резюме источника (требование ТЗ об отметке перевода)."""

    ORIGINAL_RU = "ORIGINAL_RU"
    GENERATIVE_SUMMARY = "GENERATIVE_SUMMARY"
    EXTRACTIVE = "EXTRACTIVE"


class Purpose(StrEnum):
    """Назначение вызова LLM (колонка `llm_calls.purpose`)."""

    INSIGHT = "insight"
    EXPAND = "expand"
    HEALTHCHECK = "healthcheck"
    JUDGE = "judge"


class CallStatus(StrEnum):
    """Итог вызова LLM (колонка `llm_calls.status`)."""

    OK = "OK"
    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    GROUNDING_REJECTED = "GROUNDING_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    BLOCKED = "BLOCKED"


class ProviderName(StrEnum):
    """Допустимые провайдеры (CHECK таблиц `insights` и конфигурация)."""

    GIGACHAT = "gigachat"
    YANDEXGPT = "yandexgpt"
    LOCAL_LLAMACPP = "local_llamacpp"
    FAKE = "fake"
    NONE = "none"


class Decision(StrEnum):
    """Решение analyzer по кандидату (`analyzer.v1.Decision`), нужное промпту."""

    WEAK_SIGNAL = "WEAK_SIGNAL"
    MATURE = "MATURE"
    HYPE_OR_NOISE = "HYPE_OR_NOISE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OFF_TOPIC = "OFF_TOPIC"


class TrustLevel(StrEnum):
    """Уровень доверенности источника."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Числовой ранг для выбора лучшего документа."""
        return {TrustLevel.HIGH: 3, TrustLevel.MEDIUM: 2, TrustLevel.LOW: 1}[self]


class FeatureDirection(StrEnum):
    """Направление вклада признака."""

    SUPPORTS_WEAK_SIGNAL = "supports_weak_signal"
    SUPPORTS_MATURE = "supports_mature"
    NEUTRAL = "neutral"

    @property
    def label_ru(self) -> str:
        """Русское пояснение направления для промпта и экстрактивного объяснения."""
        return {
            FeatureDirection.SUPPORTS_WEAK_SIGNAL: "поддерживает слабый сигнал",
            FeatureDirection.SUPPORTS_MATURE: "указывает на зрелость",
            FeatureDirection.NEUTRAL: "нейтрально",
        }[self]


@dataclass(frozen=True, slots=True)
class Provenance:
    """Происхождение ответа: провайдер, модель, версия промпта и расход токенов."""

    provider: str = ProviderName.NONE.value
    model: str = ""
    prompt_version: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 0
    latency_ms: int = 0
