"""Общая часть адаптеров источников: лимиты, счётчик запросов, разбор дат, коды отказов.

Адаптер не выбрасывает наружу ничего, кроме `AdapterFailure` с кодом из §10.7; ошибки транспорта
переводятся в коды здесь, чтобы каждый источник занимался только своим форматом данных.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from collector.application.ports import (
    HttpClient,
    HttpPayloadError,
    HttpStatusError,
    HttpTimeoutError,
    HttpTransportError,
    RateLimiter,
)
from collector.application.request_accounting import current_request_accounting
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, SearchTerms, SourceKey
from ws_common.clock import Clock

MAX_CONSECUTIVE_RATE_LIMITS = 3
PAGE_SIZE = 50


class BaseSourceAdapter:
    """База адаптера: ограничение частоты, подсчёт HTTP-запросов, единое отображение ошибок."""

    key: SourceKey
    raw_meta_keys: frozenset[str] = frozenset()
    allowed_hosts: frozenset[str] = frozenset()

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock) -> None:
        self._http = http
        self._limiter = limiter
        self._clock = clock
        self._http_requests = 0
        self._consecutive_rate_limits = 0

    @property
    def http_requests(self) -> int:
        """Число выполненных HTTP-запросов адаптера."""
        return self._http_requests

    def deadline_reached(self, deadline: float) -> bool:
        """Исчерпан ли общий бюджет времени сбора."""
        return self._clock.monotonic() >= deadline

    async def fetch_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """JSON-запрос с соблюдением лимита источника."""
        return await self._fetch(url, params=params, headers=headers, as_json=True)

    async def fetch_text(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Текстовый запрос (XML/Atom/RSS) с соблюдением лимита источника."""
        return str(await self._fetch(url, params=params, headers=headers, as_json=False))

    async def fetch_json_post(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST-запрос с JSON-телом (параметры поиска в теле) с соблюдением лимита источника."""
        return await self._fetch(url, params=None, headers=headers, as_json=True, json_body=json_body)

    async def _fetch(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        as_json: bool,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Общий путь запроса: лимитер → HTTP → перевод ошибок в `AdapterFailure`."""
        await self._limiter.acquire(self.key)
        self._http_requests += 1
        accounting = current_request_accounting()
        if accounting is not None:
            accounting.count_request(self.key)
        try:
            if json_body is not None:
                payload = await self._http.post_json(
                    url, allowed_hosts=self.allowed_hosts, json_body=json_body, headers=headers
                )
            elif as_json:
                payload = await self._http.get_json(
                    url, allowed_hosts=self.allowed_hosts, params=params, headers=headers
                )
            else:
                payload = await self._http.get_text(
                    url, allowed_hosts=self.allowed_hosts, params=params, headers=headers
                )
        except HttpStatusError as exc:
            await self._on_status_error(exc)
            raise AssertionError("unreachable")  # pragma: no cover - _on_status_error всегда бросает
        except HttpTimeoutError as exc:
            raise AdapterFailure(AdapterErrorCode.TIMEOUT.value, f"таймаут запроса к источнику: {exc}") from exc
        except HttpPayloadError as exc:
            raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, f"ответ источника не разобран: {exc}") from exc
        except HttpTransportError as exc:
            raise AdapterFailure(AdapterErrorCode.HTTP_5XX.value, f"источник недоступен: {exc}") from exc
        self._reset_rate_limit()
        return payload

    async def _on_status_error(self, exc: HttpStatusError) -> None:
        """429 → штраф лимитеру и счётчик подряд; прочие коды → HTTP_4XX/HTTP_5XX."""
        if exc.status == 429:
            streak = self._bump_rate_limit()
            await self._limiter.penalize(self.key, exc.retry_after)
            if streak >= MAX_CONSECUTIVE_RATE_LIMITS:
                raise AdapterFailure(
                    AdapterErrorCode.RATE_LIMITED.value,
                    f"источник ограничил частоту запросов: {MAX_CONSECUTIVE_RATE_LIMITS} ответа 429 подряд",
                )
            raise AdapterFailure(AdapterErrorCode.RATE_LIMITED.value, "источник ответил 429")
        code = AdapterErrorCode.HTTP_4XX if exc.status < 500 else AdapterErrorCode.HTTP_5XX
        raise AdapterFailure(code.value, f"источник ответил кодом {exc.status}")

    def _bump_rate_limit(self) -> int:
        """Серия ответов 429 подряд: в пределах текущего сбора, вне сбора — экземпляра адаптера."""
        accounting = current_request_accounting()
        if accounting is not None:
            return accounting.bump_rate_limit(self.key)
        self._consecutive_rate_limits += 1
        return self._consecutive_rate_limits

    def _reset_rate_limit(self) -> None:
        """Успешный ответ обрывает серию ответов 429."""
        accounting = current_request_accounting()
        if accounting is not None:
            accounting.reset_rate_limit(self.key)
        else:
            self._consecutive_rate_limits = 0


def english_first(terms: SearchTerms) -> tuple[tuple[str, str], ...]:
    """Фразы для англоязычных источников (arXiv, GitHub): только английские, если они есть.

    Русская фраза в таких источниках почти всегда даёт пустую выдачу, но расходует лимит запросов
    (arXiv — один запрос в три секунды) и общий бюджет времени сбора.
    """
    pairs = terms.pairs()
    english = tuple(pair for pair in pairs if pair[1] == "en")
    return english or pairs


def parse_datetime(value: str | None) -> datetime | None:
    """Разбор даты публикации: ISO 8601 (в т. ч. `Z`), `YYYY-MM-DD`, RFC 2822. None при неудаче."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def within_since_year(published_at: datetime | None, since_year: int) -> bool:
    """Фильтр по году публикации; документы без даты пропускаются (дату проверит доверенность)."""
    return published_at is None or published_at.year >= since_year
