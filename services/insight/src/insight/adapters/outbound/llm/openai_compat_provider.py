"""Провайдер поверх OpenAI-совместимого API: YandexGPT и локальный llama.cpp (§8 HANDOFF)."""

from __future__ import annotations

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

CHAT_PATH = "/chat/completions"
RATE_LIMIT_STATUS = 429
BLOCKED_STATUSES = frozenset({451})
log = get_logger("insight.provider.openai_compat")


class OpenAiCompatProvider:
    """Реализация порта `LLMProvider` через HTTP API формата OpenAI."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "",
        auth_scheme: str = "Bearer",
        max_concurrency: int = 1,
        supports_json_mode: bool = True,
        client: Any = None,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._auth_scheme = auth_scheme
        self._max_concurrency = max_concurrency
        self._supports_json_mode = supports_json_mode
        self._client = client

    @property
    def name(self) -> str:
        """Имя провайдера."""
        return self._name

    @property
    def model(self) -> str:
        """Идентификатор модели."""
        return self._model

    @property
    def max_concurrency(self) -> int:
        """Разрешённое число одновременных вызовов."""
        return self._max_concurrency

    def _http(self) -> Any:
        """Ленивая инициализация HTTP-клиента: httpx нужен только в рабочем контуре."""
        if self._client is None:
            import httpx  # noqa: PLC0415 - необязательная зависимость

            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        """Заголовки запроса; ключ не логируется и не попадает в сообщения об ошибках."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"{self._auth_scheme} {self._api_key}"
        return headers

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        temperature: float,
        deadline_seconds: float,
    ) -> LLMResult:
        """Вызывает `/chat/completions` и возвращает текст ответа."""
        import httpx  # noqa: PLC0415 - исключения httpx нужны для классификации ошибок

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None and self._supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            response = await self._http().post(
                f"{self._base_url}{CHAT_PATH}",
                json=body,
                headers=self._headers(),
                timeout=deadline_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderTimeout("провайдер не ответил за отведённое время", self._name) from error
        except httpx.HTTPError as error:
            raise ProviderError(f"ошибка транспорта: {type(error).__name__}", self._name) from error
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(response)
        payload = response.json()
        return _to_result(payload, self._model, latency_ms)

    def _raise_for_status(self, response: Any) -> None:
        """Переводит коды ответа в доменные ошибки, не раскрывая тело запроса."""
        status = response.status_code
        if status == RATE_LIMIT_STATUS:
            retry_after = response.headers.get("Retry-After")
            raise ProviderRateLimited(
                "провайдер ограничил частоту запросов",
                self._name,
                float(retry_after) if retry_after and retry_after.isdigit() else 5.0,
            )
        if status in BLOCKED_STATUSES:
            raise ProviderBlocked("запрос отклонён политиками провайдера", self._name)
        if status >= 400:
            raise ProviderError(f"провайдер вернул HTTP {status}", self._name)

    async def healthcheck(self) -> bool:
        """Лёгкая проверка доступности: список моделей без расхода токенов."""
        try:
            response = await self._http().get(
                f"{self._base_url}/models", headers=self._headers(), timeout=5.0
            )
        except Exception as error:  # noqa: BLE001 - проверка здоровья не должна поднимать исключения
            log.warning("provider.state", provider=self._name, healthy=False, reason=str(error)[:120])
            return False
        return response.status_code < 400


def _to_result(payload: dict[str, Any], model: str, latency_ms: int) -> LLMResult:
    """Ответ API → результат вызова; отсутствие текста считается ошибкой провайдера."""
    choices = payload.get("choices") or []
    if not choices:
        raise ProviderError("ответ провайдера не содержит вариантов")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    if not text.strip():
        raise ProviderBlocked("провайдер вернул пустой ответ")
    usage = payload.get("usage") or {}
    return LLMResult(
        text=text,
        model=payload.get("model") or model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_ms=latency_ms,
    )
