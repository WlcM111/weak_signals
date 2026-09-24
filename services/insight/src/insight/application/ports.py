"""Порты insight (`typing.Protocol`). Асинхронные: grpc.aio, psycopg async, HTTP-клиенты."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from insight.application.dto import (
    CallRecord,
    LLMMessage,
    LLMResult,
    PromptDefinition,
    ProviderState,
)
from insight.domain.entities import Insight, QueryExpansion


class LLMProvider(Protocol):
    """Внешняя языковая модель. Реализации не знают о домене insight."""

    @property
    def name(self) -> str:
        """Идентификатор провайдера (`gigachat`, `yandexgpt`, `local_llamacpp`, `fake`)."""

    @property
    def model(self) -> str:
        """Идентификатор модели, который попадёт в журнал и в ответ клиенту."""

    @property
    def max_concurrency(self) -> int:
        """Разрешённое число одновременных вызовов (для GigaChat физлица — 1)."""

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        temperature: float,
        deadline_seconds: float,
    ) -> LLMResult:
        """Выполняет запрос к модели; ошибки поднимаются типами из `domain.errors`."""

    async def healthcheck(self) -> bool:
        """Лёгкая проверка доступности (без расхода бюджета, если возможно)."""


class InsightRepository(Protocol):
    """Хранилище инсайтов (таблицы `insights`, `insight_sources`)."""

    async def get_by_idempotency_key(self, key: str) -> Insight | None:
        """Инсайт по ключу идемпотентности."""

    async def get_by_input_hash(self, input_hash: str, not_older_than: datetime) -> Insight | None:
        """Свежий инсайт с тем же входом (кеш по содержимому)."""

    async def save(self, insight: Insight) -> Insight:
        """Одной транзакцией пишет инсайт и резюме источников; повтор по ключу не дублирует."""


class ExpansionRepository(Protocol):
    """Кеш расширений запроса (таблица `query_expansions`)."""

    async def get(self, query_norm: str) -> QueryExpansion | None:
        """Сохранённое расширение по нормализованному запросу."""

    async def save(self, expansion: QueryExpansion, prompt_version: str) -> None:
        """Сохраняет расширение (повтор обновляет запись)."""


class LLMCallLog(Protocol):
    """Журнал вызовов LLM (таблица `llm_calls`)."""

    async def record(self, entry: CallRecord) -> None:
        """Пишет метаданные вызова; тексты промптов и ответов не сохраняются."""

    async def tokens_used_today(self, now: datetime) -> int:
        """Сумма токенов за текущие сутки (для суточного бюджета)."""


class PromptRegistry(Protocol):
    """Реестр версий промптов (таблица `prompt_versions`)."""

    async def register(self, definitions: Sequence[PromptDefinition]) -> None:
        """Регистрирует версии промптов при старте сервиса."""


class MetricsSink(Protocol):
    """Приём метрик insight."""

    def llm_call(self, provider: str, status: str) -> None:
        """Вызов LLM с итоговым статусом."""

    def llm_tokens(self, provider: str, kind: str, count: int) -> None:
        """Расход токенов."""

    def llm_latency(self, provider: str, seconds: float) -> None:
        """Длительность вызова."""

    def insight_status(self, status: str) -> None:
        """Статус выданного инсайта."""

    def provider_healthy(self, provider: str, healthy: bool) -> None:
        """Состояние провайдера."""

    def daily_tokens(self, count: int) -> None:
        """Израсходовано токенов за сутки."""


class NullMetrics:
    """Метрики отключены (тесты)."""

    def llm_call(self, provider: str, status: str) -> None:
        """Ничего не делает."""

    def llm_tokens(self, provider: str, kind: str, count: int) -> None:
        """Ничего не делает."""

    def llm_latency(self, provider: str, seconds: float) -> None:
        """Ничего не делает."""

    def insight_status(self, status: str) -> None:
        """Ничего не делает."""

    def provider_healthy(self, provider: str, healthy: bool) -> None:
        """Ничего не делает."""

    def daily_tokens(self, count: int) -> None:
        """Ничего не делает."""


ProviderStates = Sequence[ProviderState]
