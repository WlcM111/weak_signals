"""Детерминированный провайдер для тестов и CI (`WS_ENV=test`).

Возвращает валидный ответ по схеме, собранный из переданных доказательств: это позволяет
проверять весь путь (схема → grounding → запись) без сети и без расхода токенов.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from insight.application.dto import LLMMessage, LLMResult
from insight.domain.errors import ProviderError, ProviderRateLimited, ProviderTimeout

FAKE_MODEL = "fake-deterministic-1"


@dataclass
class FakeProvider:
    """Реализация порта `LLMProvider` без внешних вызовов."""

    name_value: str = "fake"
    model_value: str = FAKE_MODEL
    concurrency: int = 4
    responses: list[str] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    calls: list[Sequence[LLMMessage]] = field(default_factory=list)
    healthy: bool = True

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return self.name_value

    @property
    def model(self) -> str:
        """Идентификатор модели."""
        return self.model_value

    @property
    def max_concurrency(self) -> int:
        """Разрешённое число одновременных вызовов."""
        return self.concurrency

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        temperature: float,
        deadline_seconds: float,
    ) -> LLMResult:
        """Отдаёт следующий заготовленный ответ или ошибку; иначе — ответ из доказательств."""
        self.calls.append(messages)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = build_answer(messages, json_schema)
        return LLMResult(
            text=text,
            model=self.model_value,
            prompt_tokens=sum(len(message.content) // 4 for message in messages),
            completion_tokens=len(text) // 4,
            latency_ms=5,
        )

    async def healthcheck(self) -> bool:
        """Состояние провайдера."""
        return self.healthy


def build_answer(messages: Sequence[LLMMessage], json_schema: dict | None) -> str:
    """Строит валидный ответ из содержимого запроса (доказательства и признаки)."""
    payload = _user_payload(messages)
    if payload is None or "evidence" not in payload:
        return json.dumps(
            {"ru_terms": ["тестовая фраза"], "en_terms": ["test phrase"], "domain_tags": []},
            ensure_ascii=False,
        )
    evidence = payload["evidence"]
    candidate = payload.get("candidate", {})
    features = candidate.get("top_features", [])
    first = evidence[0]
    labels = ", ".join(
        f"{feature['label_ru']} = {feature['value']}" for feature in features[:2]
    ) or "признаки не переданы"
    return json.dumps(
        {
            "title_ru": candidate.get("title", "Технология"),
            "description_ru": (
                f"Источники описывают раннюю стадию развития технологии [doc:1]. "
                f"{first['title']} подтверждает наличие работ по направлению [doc:1]."
            ),
            "advantage_ru": (
                "Потенциальное преимущество — раннее выявление направления до формирования рынка."
            ),
            "case_example": {
                "text_ru": f"Пример из источника: {first['title']} [doc:1].",
                "document_id": first["document_id"],
            },
            "explanation_ru": (
                f"Модель отнесла наблюдение к слабому сигналу по признакам: {labels}. "
                "Значения признаков поддерживают раннюю стадию развития технологии."
            ),
            "source_summaries": [
                {
                    "document_id": document["document_id"],
                    "summary_ru": f"Источник описывает: {document['title']}.",
                }
                for document in evidence
            ],
        },
        ensure_ascii=False,
    )


def _user_payload(messages: Sequence[LLMMessage]) -> dict | None:
    """Разбирает пользовательское сообщение промпта (в нём JSON с данными)."""
    for message in reversed(list(messages)):
        if message.role == "user":
            try:
                return json.loads(message.content)
            except json.JSONDecodeError:
                return None
    return None


def rate_limited(retry_after: float = 0.1) -> ProviderRateLimited:
    """Готовая ошибка лимита для тестов."""
    return ProviderRateLimited("429 от провайдера", "fake", retry_after)


def timeout() -> ProviderTimeout:
    """Готовая ошибка тайм-аута для тестов."""
    return ProviderTimeout("провайдер не ответил вовремя", "fake")


def failure() -> ProviderError:
    """Готовая ошибка провайдера для тестов."""
    return ProviderError("провайдер вернул ошибку", "fake")
