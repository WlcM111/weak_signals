"""Общая часть адаптеров источников: лимиты, счётчик запросов, разбор дат, коды отказов.

Адаптер не выбрасывает наружу ничего, кроме `AdapterFailure` с кодом из §10.7; ошибки транспорта
переводятся в коды здесь, чтобы каждый источник занимался только своим форматом данных.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from collector.application.ports import (
    HttpClient,
    HttpPayloadError,
    HttpStatusError,
    HttpTimeoutError,
    HttpTransportError,
    RateLimiter,
)

if TYPE_CHECKING:
    from collector.domain.entities import RawDocument
from collector.application.request_accounting import current_request_accounting
from collector.application.trace_recording import current_collection_trace
from collector.domain.source_policy import SourcePolicy
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, SearchTerms, SourceKey
from ws_common.clock import Clock

MAX_CONSECUTIVE_RATE_LIMITS = 3
# Этап 4.7: пауза дольше 10 минут — суточный лимит, а не частота; ждать её внутри сбора бессмысленно.
LONG_BLOCK_SECONDS = 600.0
DAILY_LIMIT_PAUSE = 3600.0


def daily_limit_exhausted(exc: "HttpStatusError") -> bool:
    """429 из-за суточного лимита: Retry-After не меньше 10 минут или нулевой остаток денежного бюджета."""
    if exc.retry_after is not None and exc.retry_after >= LONG_BLOCK_SECONDS:
        return True
    return exc.rate_headers.get("x-ratelimit-remaining-usd", "").strip() in ("0", "0.0", "0.00", "0.000")
TRANSIENT_RETRY_PAUSE = 2.0  # общая пауза источника перед повтором после сетевого сбоя, таймаута или 5xx
QUOTA_RESET_MAX_WAIT = 120.0  # дольше квотного сброса GitHub внутри сбора не ждём
TRANSIENT_CODES = frozenset({"TIMEOUT", "HTTP_5XX", "NETWORK_ERROR"})
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

    # Бюджет запросов и паузы источника (аудит B2-b); main.py назначает политику из настроек.
    policy: SourcePolicy = SourcePolicy()
    # Этап 4: источник умеет прицельный поиск по технологиям (по релевантности в окне дат).
    supports_targeted: bool = False

    async def search_targeted(
        self, technologies: Sequence[str], per_term: int, since: date, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """По умолчанию источник прицельный поиск не поддерживает."""
        return
        yield  # pragma: no cover — делает метод асинхронным генератором

    def requests_made(self) -> int:
        """Логических запросов источника в текущем сборе (учёт сбора), иначе — за жизнь адаптера."""
        accounting = current_request_accounting()
        return accounting.requests_of(self.key) if accounting is not None else self._http_requests

    async def _fetch(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        as_json: bool,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Запрос по политике источника (B2-b): бюджет запросов сбора и повтор после 429 с паузой.

        Каждая попытка — отдельный логический запрос: она расходует бюджет и пишется в трассу сбора.
        """
        rate_attempt = transient_attempt = 0
        while True:
            blocked = self._limiter.blocked_for(self.key)
            if blocked >= LONG_BLOCK_SECONDS:
                # этап 4.7: источник на долгой паузе (суточный лимит) — отказ сразу, без ожидания до дедлайна сбора
                raise AdapterFailure(AdapterErrorCode.RATE_LIMITED.value,
                                     f"суточный лимит источника исчерпан; сброс через {blocked / 60:.0f} мин",
                                     retryable=False)
            if self.policy.max_requests and self.requests_made() >= self.policy.max_requests:
                raise AdapterFailure(AdapterErrorCode.BUDGET_EXHAUSTED.value,
                                     f"бюджет запросов источника на сбор исчерпан: {self.policy.max_requests}")
            try:
                return await self._fetch_traced(url, params=params, headers=headers, as_json=as_json, json_body=json_body)
            except AdapterFailure as failure:
                # Единственный владелец повторов (этап 4.2): каждая попытка заново проходит лимитер источника.
                if failure.code == AdapterErrorCode.RATE_LIMITED.value and failure.retryable:
                    if rate_attempt >= self.policy.rate_limit_retries:
                        raise
                    rate_attempt += 1
                elif failure.code in TRANSIENT_CODES and failure.retryable:
                    if transient_attempt >= self.policy.transient_retries:
                        raise
                    transient_attempt += 1
                    self._limiter.defer(self.key, TRANSIENT_RETRY_PAUSE)
                else:
                    raise

    async def _fetch_traced(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        as_json: bool,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Запрос с записью в трассу сбора (аудит A3): разобранный ответ или код отказа, без заголовков и секретов.

        Время запроса включает ожидание лимитера источника. Внутренние повторы HTTP-клиента не пишутся отдельно:
        запись отражает итог логического запроса, который увидел адаптер.
        """
        trace = current_collection_trace()
        if trace is None:
            return await self._fetch_inner(url, params=params, headers=headers, as_json=as_json, json_body=json_body)
        started_at = self._clock.now()
        began = time.perf_counter()
        common = {"source_key": self.key.value, "method": "POST" if json_body is not None else "GET", "url": url,
                  "params": params, "json_body": json_body, "started_at": started_at}
        try:
            payload = await self._fetch_inner(url, params=params, headers=headers, as_json=as_json, json_body=json_body)
        except AdapterFailure as failure:
            trace.add_response(**common, outcome=failure.code, error_message=failure.message, payload=None,
                               as_json=as_json, duration_ms=_elapsed_ms(began))
            raise
        except BaseException as error:  # отмена по бюджету и непредвиденные сбои тоже видны в трассе
            outcome = "CANCELLED" if isinstance(error, asyncio.CancelledError) else "ERROR"
            trace.add_response(**common, outcome=outcome, error_message=f"{type(error).__name__}: {error}",
                               payload=None, as_json=as_json, duration_ms=_elapsed_ms(began))
            raise
        trace.add_response(**common, outcome="OK", error_message=None, payload=payload,
                           as_json=as_json or json_body is not None, duration_ms=_elapsed_ms(began))
        return payload

    async def _fetch_inner(
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
            raise AdapterFailure(AdapterErrorCode.TIMEOUT.value, f"таймаут запроса к источнику: {exc}",
                                 retryable=True) from exc
        except HttpPayloadError as exc:
            raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, f"ответ источника не разобран: {exc}") from exc
        except HttpTransportError as exc:
            # DNS, соединение, TLS, запрещённый редирект: HTTP-ответа не было — это не 5xx (этап 4.2)
            raise AdapterFailure(AdapterErrorCode.NETWORK_ERROR.value, f"источник недоступен: {exc}",
                                 retryable="не разрешён" not in str(exc)) from exc
        self._reset_rate_limit()
        return payload

    async def _on_status_error(self, exc: HttpStatusError) -> None:
        """429 и квотный 403 → общая пауза лимитера и счётчик подряд; прочие коды → HTTP_4XX/HTTP_5XX."""
        quota_reset = quota_exhausted_wait(exc, time.time())
        if exc.status == 429 and daily_limit_exhausted(exc):
            # этап 4.7: суточный лимит (OpenAlex: Retry-After до полуночи UTC, X-RateLimit-Remaining-USD: 0) — пауза
            # источника до сброса для всех задач процесса и отказ без повторов
            pause = exc.retry_after if exc.retry_after is not None else DAILY_LIMIT_PAUSE
            await self._limiter.penalize(self.key, pause)
            raise AdapterFailure(AdapterErrorCode.RATE_LIMITED.value,
                                 f"суточный лимит источника исчерпан; сброс через {pause / 60:.0f} мин", retryable=False)
        if exc.status == 429 and self.policy.cooldown_seconds > 0:
            # этап 5.0 (F05): источник с частыми 429 (GDELT) уходит на паузу для всех сборов процесса
            pause = max(float(exc.retry_after or 0.0), self.policy.cooldown_seconds)
            await self._limiter.penalize(self.key, pause)
            raise AdapterFailure(AdapterErrorCode.RATE_LIMITED.value,
                                 f"источник ограничил частоту; пауза {pause / 60:.0f} мин", retryable=False)
        if exc.status == 429 or quota_reset is not None:
            streak = self._bump_rate_limit()
            pause = exc.retry_after if exc.retry_after is not None else quota_reset
            if pause is None:
                pause = self.policy.rate_limit_pause or None
            await self._limiter.penalize(self.key, pause)
            if streak >= MAX_CONSECUTIVE_RATE_LIMITS:
                raise AdapterFailure(
                    AdapterErrorCode.RATE_LIMITED.value,
                    f"источник ограничил частоту запросов: {MAX_CONSECUTIVE_RATE_LIMITS} ответа 429 подряд",
                )
            raise AdapterFailure(AdapterErrorCode.RATE_LIMITED.value, "источник ответил 429", retryable=True)
        code = AdapterErrorCode.HTTP_4XX if exc.status < 500 else AdapterErrorCode.HTTP_5XX
        raise AdapterFailure(code.value, f"источник ответил кодом {exc.status}", retryable=exc.status >= 500,
                             http_status=exc.status)

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


def _elapsed_ms(began: float) -> int:
    """Миллисекунды от отметки `time.perf_counter()`."""
    return int((time.perf_counter() - began) * 1000)


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


# Окна истории для новостных источников (GDELT, Hacker News): поиск по 90-дневным окнам от новых к старым,
# квота документов источника делится поровну на запросы, чтобы старые окна не вытеснялись лимитом.
HISTORY_WINDOW_DAYS = 90
DAYS_PER_MONTH = 30.44


def history_windows(now: datetime, months: int, since_year: int) -> list[tuple[datetime, datetime]]:
    """Окна поиска по 90 дней от `now` назад: не глубже `months` месяцев и не раньше 1 января `since_year`."""
    start_limit = now - timedelta(days=round(months * DAYS_PER_MONTH))
    if since_year:
        start_limit = max(start_limit, datetime(since_year, 1, 1, tzinfo=UTC))
    windows: list[tuple[datetime, datetime]] = []
    end = now
    while end > start_limit:
        start = max(start_limit, end - timedelta(days=HISTORY_WINDOW_DAYS))
        windows.append((start, end))
        end = start
    return windows


def records_per_request(quota: int, planned: int, minimum: int = 10, maximum: int = 250) -> int:
    """Квота документов источника поровну на запросы, в пределах `minimum`…`maximum` записей за запрос."""
    if planned <= 0:
        return minimum
    return max(minimum, min(maximum, math.ceil(quota / planned)))


def quota_exhausted_wait(exc: HttpStatusError, now_epoch: float) -> float | None:
    """403 с исчерпанной квотой (GitHub: `x-ratelimit-remaining: 0` или `Retry-After`) → секунды до сброса, иначе None.

    GitHub сообщает исчерпание поисковой квоты кодом 403 или 429 (документация Search API); обычный 403 — отказ.
    """
    if exc.status != 403:
        return None
    if exc.retry_after is not None:
        return min(exc.retry_after, QUOTA_RESET_MAX_WAIT)
    if exc.rate_headers.get("x-ratelimit-remaining") != "0":
        return None
    try:
        reset = float(exc.rate_headers.get("x-ratelimit-reset", ""))
    except ValueError:
        return DEFAULT_QUOTA_WAIT
    return min(max(1.0, reset - now_epoch), QUOTA_RESET_MAX_WAIT)


DEFAULT_QUOTA_WAIT = 60.0
