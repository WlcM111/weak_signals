"""Адаптер GDELT DOC 2.0: мировые новости о пилотах, контрактах, запусках и финансировании.

Деловые сигналы — страхование роботов, маркетплейсы навыков, роботы как услуга — появляются в новостях
о пилотах и сделках раньше, чем в научных статьях. Адаптер ищет английские фразы темы вместе с деловыми
маркерами в режиме `artlist`. Документ — заголовок новости: текст статей API не отдаёт. Тип источника
INDUSTRY_MEDIA, доверенность MEDIUM — новость подсказывает сигнал, но не подтверждает его в одиночку.

История (этап 3). DOC API ищет с 1 января 2017 года, но в режиме artlist отдаёт статьи только из последних трёх
месяцев окна поиска (блог GDELT «DOC 2.0 Updates: 1.5 Year Searching»). Поэтому вместо `timespan=3months` адаптер
идёт окнами по 90 дней через STARTDATETIME/ENDDATETIME — от новых к старым, на глубину `history_months`.
Квота документов источника делится между запросами поровну, чтобы старые окна не вытеснялись лимитом.

Частота. GDELT требует не чаще одного запроса в 5 с (ответ 429 «Please limit requests to one every 5 seconds»),
поэтому запросы идут через общий шлюз процесса: один одновременно, не чаще раза в 5,5 с. Пауза — только после
признаков перегрузки (429 или текст вместо JSON) и короткая; если она кончается до срока сбора, адаптер ждёт.
Таймаут соединения (стенд 04.10.2026: «таймаут запроса» через 5 с в каждой теме) — повтор того же окна после
интервала шлюза, без паузы для следующих сборов: прежняя 10-минутная пауза после таймаута отключала GDELT
у следующих тем, и он не опрашивался вовсе.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from collector.adapters.outbound.sources.base import (
    BaseSourceAdapter,
    english_first,
    history_windows,
    records_per_request,
    within_since_year,
)
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import SourcePolicy
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250  # предел DOC API для artlist
MIN_RECORDS = 10
DEFAULT_HISTORY_MONTHS = 24
MAX_TERMS = 2
MIN_GAP_SECONDS = 5.5
COOLDOWN_SECONDS = 120
TIMEOUT_RETRIES = 1
# Не ждать паузу шлюза, если после неё на запрос остаётся меньше этого запаса до срока сбора.
DEADLINE_MARGIN_SECONDS = 15.0
# Маркеры деловых событий: без них по фразе темы приходят обзоры и мнения, а не пилоты и сделки.
BUSINESS_MARKERS = ("pilot", "funding", "startup", "contract", "launch", "partnership", "deployment")
# Пауза для следующих запросов — только при признаках перегрузки GDELT: 429 или текст вместо JSON.
COOLDOWN_CODES = frozenset({AdapterErrorCode.RATE_LIMITED.value, AdapterErrorCode.PARSE_ERROR.value})
_LANGUAGES = {
    "english": "en", "russian": "ru", "german": "de", "french": "fr", "spanish": "es",
    "chinese": "zh", "japanese": "ja", "italian": "it", "portuguese": "pt",
}
_QUOTE_RE = re.compile(r'["\\]')

log = get_logger("collector.gdelt")


class _GdeltGate:
    """Общий на процесс шлюз к GDELT: один запрос одновременно, интервал и пауза после отказа."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_request_at: float | None = None
        self.blocked_until = 0.0
        self.blocked_code = ""
        self.blocked_reason = ""

    def reset(self) -> None:
        """Сброс состояния; замок пересоздаётся, чтобы не остаться привязанным к прежнему циклу событий."""
        self.lock = asyncio.Lock()
        self.last_request_at = None
        self.blocked_until = 0.0
        self.blocked_code = ""
        self.blocked_reason = ""


_GATE = _GdeltGate()


def reset_gdelt_gate() -> None:
    """Сбрасывает интервал и паузу общего шлюза GDELT (для тестов)."""
    _GATE.reset()


class GdeltAdapter(BaseSourceAdapter):
    """Поиск деловых новостей GDELT по фразам запроса, окнами истории."""

    key = SourceKey.GDELT
    # Этап 4.2: запросы GDELT не менялись; повтор после таймаута — только собственный повтор окна адаптера.
    policy = SourcePolicy(max_requests=8, transient_retries=0)
    raw_meta_keys = frozenset({"domain", "source_country"})
    allowed_hosts = frozenset({"api.gdeltproject.org"})

    def __init__(
        self, http: HttpClient, limiter: RateLimiter, clock: Clock, history_months: int = DEFAULT_HISTORY_MONTHS
    ) -> None:
        super().__init__(http, limiter, clock)
        self._history_months = history_months

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Новости по английским фразам темы: каждая фраза — по окнам от новых к старым, квота делится поровну."""
        phrases = [term for term, _language in english_first(terms)[:MAX_TERMS]]
        windows = history_windows(self._clock.now(), self._history_months, limits.published_since_year)
        planned = len(phrases) * len(windows)
        if self.policy.max_requests:
            planned = min(planned, self.policy.max_requests)
        records = records_per_request(limits.max_documents_per_source, planned, MIN_RECORDS, MAX_RECORDS)
        produced = 0
        failures: list[AdapterFailure] = []
        attempted = 0
        for term in phrases:
            for start, end in windows:
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    break
                if self.policy.max_requests and self.requests_made() >= self.policy.max_requests:
                    break  # бюджет запросов на сбор израсходован (вместе с повторами) — плановая остановка, не отказ
                attempted += 1
                try:
                    articles = await self._fetch_articles(
                        {
                            "query": business_query(term),
                            "mode": "artlist",
                            "format": "json",
                            "maxrecords": records,
                            "startdatetime": gdelt_datetime(start),
                            "enddatetime": gdelt_datetime(end),
                        },
                        deadline,
                    )
                except AdapterFailure as failure:
                    if failure.code == AdapterErrorCode.BUDGET_EXHAUSTED.value:
                        attempted -= 1
                        break  # бюджет кончился на повторе окна — плановая остановка, не отказ
                    failures.append(failure)
                    if failure.code in COOLDOWN_CODES:
                        break  # перегрузка, пауза которой не кончится до срока сбора, — дальше не спрашиваем
                    continue  # таймаут или 4xx/5xx одного окна не отменяет остальные окна
                for article in articles:
                    document = _to_raw_document(article, term)
                    if document is None or not within_since_year(document.published_at, limits.published_since_year):
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
            if failures and failures[-1].code in COOLDOWN_CODES:
                break
            if self.policy.max_requests and self.requests_made() >= self.policy.max_requests:
                break
        if failures:
            # Отказы видны в статусе запуска: без документов — отказ, с документами — частичный результат.
            first = failures[0]
            raise AdapterFailure(first.code, f"GDELT: не удалось {len(failures)} из {attempted} запросов; первый: {first.message}")

    async def _fetch_articles(self, params: dict[str, str | int], deadline: float) -> list[dict[str, Any]]:
        """Один запрос окна через общий шлюз; таймаут соединения или чтения — один повтор после интервала шлюза."""
        for attempt in range(TIMEOUT_RETRIES + 1):
            try:
                return await self._gated_request(params, deadline)
            except AdapterFailure as failure:
                if failure.code != AdapterErrorCode.TIMEOUT.value or attempt >= TIMEOUT_RETRIES:
                    raise
                if self._clock.monotonic() + MIN_GAP_SECONDS + DEADLINE_MARGIN_SECONDS > deadline:
                    raise
                log.info("gdelt.timeout_retry", attempt=attempt + 1)
        raise AssertionError("unreachable")

    async def _gated_request(self, params: dict[str, str | int], deadline: float) -> list[dict[str, Any]]:
        """Запрос через шлюз: интервал между запросами, ожидание паузы перегрузки, если она кончается до срока сбора."""
        async with _GATE.lock:
            now = self._clock.monotonic()
            if now < _GATE.blocked_until:
                if _GATE.blocked_until + DEADLINE_MARGIN_SECONDS > deadline:
                    raise AdapterFailure(
                        _GATE.blocked_code,
                        f"GDELT не опрашивается: пауза после перегрузки ({_GATE.blocked_reason}) "
                        f"ещё {int(_GATE.blocked_until - now)} с — дольше, чем осталось до срока сбора",
                    )
                await self._clock.sleep(_GATE.blocked_until - now)
                now = self._clock.monotonic()
            if _GATE.last_request_at is not None:
                wait = _GATE.last_request_at + MIN_GAP_SECONDS - now
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                return _articles(await self.fetch_json(API_URL, params=params))
            except AdapterFailure as failure:
                if failure.code in COOLDOWN_CODES:
                    _GATE.blocked_until = self._clock.monotonic() + COOLDOWN_SECONDS
                    _GATE.blocked_code = failure.code
                    _GATE.blocked_reason = failure.message
                    log.warning("gdelt.cooldown", code=failure.code, seconds=COOLDOWN_SECONDS)
                raise
            finally:
                _GATE.last_request_at = self._clock.monotonic()


def gdelt_datetime(value: datetime) -> str:
    """Время в формате STARTDATETIME/ENDDATETIME DOC API: YYYYMMDDHHMMSS (UTC)."""
    return value.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def business_query(term: str) -> str:
    """Фраза темы в кавычках, группа деловых маркеров и только англоязычные источники."""
    phrase = " ".join(_QUOTE_RE.sub(" ", term).split())
    return f'"{phrase}" ({" OR ".join(BUSINESS_MARKERS)}) sourcelang:english'


def _articles(payload: Any) -> list[dict[str, Any]]:
    """Статьи из ответа; пустой ответ без ключа `articles` — это «ничего не найдено»."""
    if not isinstance(payload, dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "GDELT: ответ не объект JSON")
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "GDELT: поле articles не список")
    return [article for article in articles if isinstance(article, dict)]


def _to_raw_document(article: dict[str, Any], term: str) -> RawDocument | None:
    """Статья GDELT → сырой документ; None без заголовка или ссылки."""
    title = article.get("title")
    url = article.get("url")
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url.startswith("http"):
        return None
    domain = article.get("domain")
    country = article.get("sourcecountry")
    return RawDocument(
        url=url,
        title=title.strip(),
        text=title.strip(),
        matched_term=term,
        published_at=parse_seendate(article.get("seendate")),
        language_code=_LANGUAGES.get(str(article.get("language") or "").strip().lower()),
        doi=None,
        raw_meta={
            "domain": domain if isinstance(domain, str) else "",
            "source_country": country if isinstance(country, str) else "",
        },
    )


def parse_seendate(value: object) -> datetime | None:
    """Дата вида `20260923T120000Z` → datetime в UTC; иной формат — None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
