"""Адаптер GDELT DOC 2.0: мировые новости о пилотах, контрактах, запусках и финансировании.

Деловые сигналы — страхование роботов, маркетплейсы навыков, роботы как услуга — появляются в новостях
о пилотах и сделках раньше, чем в научных статьях. Адаптер ищет английские фразы темы вместе с деловыми
маркерами в режиме `artlist` за последние три месяца (окно DOC API). Документ — заголовок новости:
текст статей API не отдаёт. Тип источника INDUSTRY_MEDIA, доверенность MEDIUM — новость подсказывает
сигнал, но не подтверждает его в одиночку.

Лимиты частоты в документации DOC API не опубликованы, поэтому запросы идут через общий шлюз процесса:
один одновременно, не чаще раза в 5,5 с, пауза после отказа. GDELT при перегрузке может ответить текстом
вместо JSON — такой ответ тоже включает паузу.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from collector.adapters.outbound.sources.base import BaseSourceAdapter, english_first, within_since_year
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.logging import get_logger

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 75
TIMESPAN = "3months"
MAX_TERMS = 2
MIN_GAP_SECONDS = 5.5
COOLDOWN_SECONDS = 10 * 60
# Маркеры деловых событий: без них по фразе темы приходят обзоры и мнения, а не пилоты и сделки.
BUSINESS_MARKERS = ("pilot", "funding", "startup", "contract", "launch", "partnership", "deployment")
COOLDOWN_CODES = frozenset(
    {
        AdapterErrorCode.RATE_LIMITED.value,
        AdapterErrorCode.HTTP_5XX.value,
        AdapterErrorCode.TIMEOUT.value,
        AdapterErrorCode.PARSE_ERROR.value,
    }
)
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
    """Поиск деловых новостей GDELT по фразам запроса."""

    key = SourceKey.GDELT
    raw_meta_keys = frozenset({"domain", "source_country"})
    allowed_hosts = frozenset({"api.gdeltproject.org"})

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Новости по английским фразам темы с деловыми маркерами; фильтр по году публикации."""
        produced = 0
        for term, _language in english_first(terms)[:MAX_TERMS]:
            if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                return
            articles = await self._fetch_articles(
                {
                    "query": business_query(term),
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": MAX_RECORDS,
                    "timespan": TIMESPAN,
                }
            )
            for article in articles:
                document = _to_raw_document(article, term)
                if document is None or not within_since_year(document.published_at, limits.published_since_year):
                    continue
                produced += 1
                yield document
                if produced >= limits.max_documents_per_source:
                    return

    async def _fetch_articles(self, params: dict[str, str | int]) -> list[dict[str, Any]]:
        """Один запрос к GDELT через общий шлюз; разбор внутри шлюза, чтобы негодный ответ включал паузу."""
        async with _GATE.lock:
            now = self._clock.monotonic()
            if now < _GATE.blocked_until:
                raise AdapterFailure(
                    _GATE.blocked_code,
                    f"GDELT не опрашивается после отказа ({_GATE.blocked_reason}), "
                    f"пауза ещё {int(_GATE.blocked_until - now)} с",
                )
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
