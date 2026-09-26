"""HTTP-клиент внешних источников на httpx (§8 HANDOFF, §14.5 ТЗ).

Свойства: connect 5 с, read из конфигурации, лимит размера ответа, ретраи на 429/5xx/таймаут
с экспоненциальной паузой и учётом `Retry-After`, белый список хостов (SSRF), вежливый User-Agent.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from collector.application.ports import (
    HttpPayloadError,
    HttpStatusError,
    HttpTimeoutError,
    HttpTransportError,
)
from collector.domain.retry_after import parse_retry_after
from collector.domain.rules import decode_body
from ws_common.logging import get_logger

CONNECT_TIMEOUT_SECONDS = 5.0
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 8.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Суммарное ожидание внутренних повторов одного логического вызова: остальное — лимитеру источника.
MAX_RETRY_SLEEP_TOTAL = 15.0


class HttpxClient:
    """Реализация порта `HttpClient`."""

    def __init__(
        self,
        contact_email: str,
        read_timeout_seconds: float,
        max_bytes: int,
        retries: int,
        max_connections_per_host: int = 4,
    ) -> None:
        self._max_bytes = max_bytes
        self._retries = retries
        self.attempts_total = 0  # HTTP-попытки, включая внутренние повторы (логические вызовы считает адаптер)
        self._log = get_logger("collector.http")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS),
            limits=httpx.Limits(
                max_connections=max_connections_per_host * 4, max_keepalive_connections=max_connections_per_host
            ),
            headers={"User-Agent": f"weak-signals/1.0 (mailto:{contact_email})"},
            follow_redirects=True,
            verify=True,
        )

    async def aclose(self) -> None:
        """Закрывает пул соединений при остановке сервиса."""
        await self._client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET с разбором JSON; неверный JSON → HttpPayloadError."""
        body = await self._get(url, allowed_hosts=allowed_hosts, params=params, headers=headers)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise HttpPayloadError(f"ответ не является JSON: {exc}") from exc

    async def get_text(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """GET с возвратом текста (XML/Atom/RSS)."""
        body = await self._get(url, allowed_hosts=allowed_hosts, params=params, headers=headers)
        return decode_body(body)

    async def post_json(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST с JSON-телом и разбором JSON; поисковый POST идемпотентен, поэтому повторы допустимы."""
        body = await self._request(
            "POST", url, allowed_hosts=allowed_hosts, params=None, headers=headers, json_body=json_body
        )
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise HttpPayloadError(f"ответ не является JSON: {exc}") from exc

    async def _get(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
    ) -> bytes:
        """GET с ретраями; возвращает тело ответа в пределах лимита размера."""
        return await self._request("GET", url, allowed_hosts=allowed_hosts, params=params, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None = None,
    ) -> bytes:
        """Выполняет запрос с ретраями; возвращает тело ответа в пределах лимита размера."""
        self._check_host(url, allowed_hosts)
        attempt = 0
        slept = 0.0
        host = (urlsplit(url).hostname or "").lower()
        while True:
            self.attempts_total += 1
            try:
                return await self._attempt(url, params, headers, method, json_body)
            except (HttpStatusError, HttpTimeoutError, HttpTransportError) as exc:
                retryable = isinstance(exc, (HttpTimeoutError, HttpTransportError)) or (
                    isinstance(exc, HttpStatusError) and exc.status in RETRYABLE_STATUSES
                )
                retry_after = exc.retry_after if isinstance(exc, HttpStatusError) else None
                if retry_after is not None and retry_after > RETRY_MAX_DELAY:
                    # Сервер просит ждать дольше внутреннего повтора: решение о паузе — у лимитера источника.
                    self._log.warning("http.retry_after_exceeds", host=host, retry_after=retry_after)
                    raise
                if not retryable or attempt >= self._retries:
                    raise
                delay = self._backoff(attempt, exc)
                if slept + delay > MAX_RETRY_SLEEP_TOTAL:
                    raise
                attempt += 1
                slept += delay
                self._log.info("http.retry", host=host, attempt=attempt, delay=round(delay, 2),
                               status=getattr(exc, "status", None), error=type(exc).__name__)
                await asyncio.sleep(delay)

    async def _attempt(
        self,
        url: str,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        method: str = "GET",
        json_body: Mapping[str, Any] | None = None,
    ) -> bytes:
        """Один HTTP-запрос с потоковым чтением и контролем размера ответа."""
        try:
            request = self._client.build_request(method, url, params=params, headers=headers, json=json_body)
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise HttpTimeoutError(f"таймаут запроса: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HttpTransportError(f"сетевая ошибка: {exc}") from exc
        try:
            if response.status_code >= 400:
                raise HttpStatusError(
                    response.status_code,
                    f"источник ответил кодом {response.status_code}",
                    _retry_after(response.headers.get("Retry-After")),
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_bytes:
                    raise HttpPayloadError(f"ответ превышает лимит {self._max_bytes} байт")
                chunks.append(chunk)
            return b"".join(chunks)
        except httpx.TimeoutException as exc:
            raise HttpTimeoutError(f"таймаут чтения: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HttpTransportError(f"сетевая ошибка чтения: {exc}") from exc
        finally:
            await response.aclose()

    def _backoff(self, attempt: int, exc: Exception) -> float:
        """Экспоненциальная пауза с полным джиттером; `Retry-After` имеет приоритет."""
        if isinstance(exc, HttpStatusError) and exc.retry_after is not None:
            return min(exc.retry_after, RETRY_MAX_DELAY)
        return random.uniform(0, min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY))  # noqa: S311 - джиттер, не крипто

    @staticmethod
    def _check_host(url: str, allowed_hosts: frozenset[str]) -> None:
        """Белый список хостов адаптера: защита от SSRF через данные источников."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"}:
            raise HttpTransportError(f"недопустимая схема URL: {parts.scheme}")
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
            raise HttpTransportError(f"хост {host} не разрешён для этого адаптера")


def _retry_after(value: str | None) -> float | None:
    """Разбирает `Retry-After` в обеих формах (секунды и HTTP-дата)."""
    return parse_retry_after(value)
