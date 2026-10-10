"""Адаптер Hacker News Search (Algolia): история технологических новостей — второй новостной поиск (этап 3.1).

GDELT на стенде отвечает 429 даже после минуты тишины («Please limit requests to one every 5 seconds…», диагностика
06.10.2026) — он ограничивает адрес стенда, и история новостей через него недоступна. Экспертные сигналы взяты в
основном из новостей о стартапах (TechCrunch, SiliconANGLE, The AI Insider…), а ссылки на такие статьи публикуют
на Hacker News.

Официальная документация HN Search API (hn.algolia.com/api): `GET /api/v1/search` — поиск по релевантности,
`tags=story` — только публикации, `numericFilters=created_at_i>X,created_at_i<Y` — окно дат (Unix-время),
`hitsPerPage` — размер страницы. Ключ не нужен; лимит — 10 000 запросов в час с одного IP. При использовании
указывается источник «Search by Algolia» — так он назван в каталоге источников.

Документ — публикация Hacker News: заголовок и ссылка на исходную статью; текста статьи API не отдаёт (у постов
Ask/Show HN — текст поста). Запрос — все слова фразы (Algolia по умолчанию требует все слова); окна по 90 дней на
глубину `history_months`, квота источника поровну на запросы. Тип документа — по домену ссылки (как у RSS),
доверенность не выше MEDIUM: агрегатор подсказывает сигнал, но не подтверждает его в одиночку.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

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
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
MAX_TERMS = 2
DEFAULT_HISTORY_MONTHS = 24
MIN_HITS = 5
MAX_HITS = 100
MAX_TEXT = 4000
_TAG_RE = re.compile(r"<[^>]+>")


class HackerNewsAdapter(BaseSourceAdapter):
    """Публикации Hacker News по английским фразам темы, окнами истории."""

    key = SourceKey.HACKERNEWS
    raw_meta_keys = frozenset({"hn_id", "points", "num_comments", "domain"})
    allowed_hosts = frozenset({"hn.algolia.com"})

    def __init__(
        self, http: HttpClient, limiter: RateLimiter, clock: Clock, history_months: int = DEFAULT_HISTORY_MONTHS
    ) -> None:
        super().__init__(http, limiter, clock)
        self._history_months = history_months

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Каждая фраза — по окнам от новых к старым; отказ окна не отменяет остальные и виден в статусе запуска."""
        phrases = [(term, hn_query(term)) for term, _language in english_first(terms)[:MAX_TERMS]]
        phrases = [(term, query) for term, query in phrases if query]
        windows = history_windows(self._clock.now(), self._history_months, limits.published_since_year)
        planned = len(phrases) * len(windows)
        if self.policy.max_requests:
            planned = min(planned, self.policy.max_requests)
        hits = records_per_request(limits.max_documents_per_source, planned, MIN_HITS, MAX_HITS)
        produced = 0
        attempted = 0
        failures: list[AdapterFailure] = []
        for term, query in phrases:
            for start, end in windows:
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    break
                if self.policy.max_requests and self.requests_made() >= self.policy.max_requests:
                    break
                attempted += 1
                try:
                    payload = await self.fetch_json(API_URL, params={
                        "query": query,
                        "tags": "story",
                        "numericFilters": f"created_at_i>{int(start.timestamp())},created_at_i<{int(end.timestamp())}",
                        "hitsPerPage": hits,
                    })
                except AdapterFailure as failure:
                    if failure.code == AdapterErrorCode.BUDGET_EXHAUSTED.value:
                        attempted -= 1
                        break
                    failures.append(failure)
                    if failure.code == AdapterErrorCode.RATE_LIMITED.value:
                        break
                    continue
                for hit in _hits(payload):
                    document = _to_raw_document(hit, term)
                    if document is None or not within_since_year(document.published_at, limits.published_since_year):
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
            if failures and failures[-1].code == AdapterErrorCode.RATE_LIMITED.value:
                break
        if failures:
            first = failures[0]
            raise AdapterFailure(first.code,
                                 f"Hacker News: не удалось {len(failures)} из {attempted} запросов; первый: {first.message}")

    supports_targeted = True

    async def search_targeted(
        self, technologies: Sequence[str], per_term: int, since: date, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Этап 4: технология — поиск по релевантности, публикации не раньше `since`."""
        start = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp())
        for term in technologies:
            query = hn_query(term)
            if not query or self.deadline_reached(deadline):
                continue
            payload = await self.fetch_json(API_URL, params={
                "query": query, "tags": "story", "numericFilters": f"created_at_i>{start}", "hitsPerPage": per_term,
            })
            taken = 0
            for hit in _hits(payload):
                document = _to_raw_document(hit, term)
                if document is None:
                    continue
                taken += 1
                yield document
                if taken >= per_term:
                    break


def hn_query(term: str) -> str:
    """Фраза запроса: слова через пробел — Algolia по умолчанию требует присутствия всех слов."""
    return " ".join(term.replace('"', " ").split())


def _hits(payload: Any) -> list[dict[str, Any]]:
    """Публикации из ответа; структурно неверный ответ → PARSE_ERROR."""
    if not isinstance(payload, dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Hacker News: ответ не объект JSON")
    hits = payload.get("hits", [])
    if not isinstance(hits, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Hacker News: поле hits не список")
    return [hit for hit in hits if isinstance(hit, dict)]


def _to_raw_document(hit: dict[str, Any], term: str) -> RawDocument | None:
    """Публикация Hacker News → сырой документ; ссылка — на исходную статью, у постов без ссылки — на обсуждение."""
    title = hit.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    url = hit.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        item_id = hit.get("objectID") or hit.get("story_id")
        if not item_id:
            return None
        url = ITEM_URL.format(item_id)
    story = hit.get("story_text")
    body = story_text(story) if isinstance(story, str) else ""
    points = hit.get("points") if isinstance(hit.get("points"), int) else None
    comments = hit.get("num_comments") if isinstance(hit.get("num_comments"), int) else None
    return RawDocument(
        url=url,
        title=title.strip(),
        text=" ".join(part for part in (title.strip(), body) if part)[:MAX_TEXT],
        matched_term=term,
        published_at=published_at(hit),
        language_code=None,
        doi=None,
        engagement_count=points,
        raw_meta={
            "hn_id": str(hit.get("objectID") or ""),
            "points": points if points is not None else 0,
            "num_comments": comments if comments is not None else 0,
            "domain": (urlsplit(url).hostname or "").lower(),
        },
    )


def story_text(value: str) -> str:
    """Текст поста Ask/Show HN: без HTML-разметки и сущностей, пробелы схлопнуты."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value)).split())


def published_at(hit: dict[str, Any]) -> datetime | None:
    """Дата публикации из `created_at_i` (Unix-время), иначе из `created_at` (ISO 8601)."""
    value = hit.get("created_at_i")
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, tz=UTC)
    text = hit.get("created_at")
    if isinstance(text, str) and text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None
