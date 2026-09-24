"""Провайдер GigaChat через официальный SDK (§8 HANDOFF).

Проверка TLS обязательна: сертификат НУЦ Минцифры передаётся через `WS_GIGACHAT_CA_BUNDLE`.
Учётные данные читаются из окружения и не попадают ни в логи, ни в сообщения об ошибках.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from insight.application.dto import LLMMessage, LLMResult
from insight.domain.errors import (
    ProviderBlocked,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from ws_common.logging import get_logger

ALLOWED_MODELS = ("GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max")
RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit")
BLOCKED_MARKERS = ("censor", "content filter", "blocked", "запрещ")
log = get_logger("insight.provider.gigachat")


def is_allowed_model(model: str) -> bool:
    """Модель из списка, разрешённого ТЗ (семейство GigaChat 2)."""
    return model in ALLOWED_MODELS


class GigaChatProvider:
    """Реализация порта `LLMProvider` поверх SDK `gigachat`."""

    def __init__(
        self,
        *,
        credentials: str,
        model: str,
        scope: str = "GIGACHAT_API_PERS",
        ca_bundle_file: str = "",
        max_concurrency: int = 1,
        client: Any = None,
    ) -> None:
        if not is_allowed_model(model):
            raise ValueError(
                f"модель {model} вне списка, разрешённого ТЗ: {', '.join(ALLOWED_MODELS)}"
            )
        self._credentials = credentials
        self._model = model
        self._scope = scope
        self._ca_bundle_file = ca_bundle_file
        self._max_concurrency = max_concurrency
        self._client = client

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return "gigachat"

    @property
    def model(self) -> str:
        """Идентификатор модели."""
        return self._model

    @property
    def max_concurrency(self) -> int:
        """Для учётной записи физлица — один поток (§18 HANDOFF)."""
        return self._max_concurrency

    def _sdk(self) -> Any:
        """Ленивая инициализация клиента SDK."""
        if self._client is None:
            from gigachat import GigaChat  # noqa: PLC0415 - тяжёлая зависимость рабочего контура

            self._client = GigaChat(
                credentials=self._credentials,
                scope=self._scope,
                model=self._model,
                ca_bundle_file=self._ca_bundle_file or None,
                verify_ssl_certs=True,
            )
        return self._client

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        temperature: float,
        deadline_seconds: float,
    ) -> LLMResult:
        """Синхронный SDK вызывается в отдельном потоке, чтобы не блокировать цикл событий."""
        payload = {
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._sdk().chat, payload), timeout=deadline_seconds
            )
        except TimeoutError as error:
            raise ProviderTimeout("GigaChat не ответил за отведённое время", self.name) from error
        except Exception as error:  # noqa: BLE001 - SDK поднимает собственные типы исключений
            raise _classify(error) from error
        latency_ms = int((time.perf_counter() - started) * 1000)
        return _to_result(response, self._model, latency_ms)

    async def healthcheck(self) -> bool:
        """Проверка доступности списком моделей (без расхода токенов)."""
        try:
            await asyncio.to_thread(self._sdk().get_models)
        except Exception as error:  # noqa: BLE001
            log.warning("provider.state", provider=self.name, healthy=False, reason=type(error).__name__)
            return False
        return True


def _classify(error: Exception) -> ProviderError:
    """Ошибка SDK → доменная ошибка; текст промпта в сообщение не попадает."""
    text = str(error).lower()
    if any(marker in text for marker in RATE_LIMIT_MARKERS):
        return ProviderRateLimited("GigaChat ограничил частоту запросов", "gigachat")
    if any(marker in text for marker in BLOCKED_MARKERS):
        return ProviderBlocked("GigaChat отклонил запрос политиками контента", "gigachat")
    return ProviderError(f"ошибка GigaChat: {type(error).__name__}", "gigachat")


def _to_result(response: Any, model: str, latency_ms: int) -> LLMResult:
    """Ответ SDK → результат вызова."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ProviderError("ответ GigaChat не содержит вариантов", "gigachat")
    text = getattr(choices[0].message, "content", "") or ""
    if not text.strip():
        raise ProviderBlocked("GigaChat вернул пустой ответ", "gigachat")
    usage = getattr(response, "usage", None)
    return LLMResult(
        text=text,
        model=getattr(response, "model", None) or model,
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        latency_ms=latency_ms,
    )
