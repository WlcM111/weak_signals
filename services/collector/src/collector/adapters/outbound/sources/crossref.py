"""Адаптер Crossref REST API (этап 4.3): журнальные статьи (Nature, MDPI, Frontiers…) по релевантности в окне дат.

`GET https://api.crossref.org/works?query.bibliographic=…&filter=from-pub-date:…&rows=N&select=…&mailto=…` —
ключ не нужен, `mailto` переводит запросы в «вежливый» пул. Широкий проход — две английские фразы темы,
прицельный — по `per_term` статей на технологию. Тип — научная публикация (trust_rules).
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any

from collector.adapters.outbound.sources.base import BaseSourceAdapter, english_first, within_since_year
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.technology_mining import targeted_since
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://api.crossref.org/works"
SELECT = "DOI,title,abstract,issued,URL,container-title,type,is-referenced-by-count"
MAX_TERMS = 2
MAX_ROWS = 100
_TAG_RE = re.compile(r"<[^>]+>")


class CrossrefAdapter(BaseSourceAdapter):
    """Журнальные статьи Crossref по фразам темы и по технологиям."""

    key = SourceKey.CROSSREF
    raw_meta_keys = frozenset({"container", "type"})
    allowed_hosts = frozenset({"api.crossref.org"})
    supports_targeted = True

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, mailto: str, window_months: int = 24) -> None:
        super().__init__(http, limiter, clock)
        self._mailto = mailto
        self._window_months = window_months

    def _params(self, query: str, rows: int, since: date) -> dict[str, str | int]:
        return {"query.bibliographic": query, "filter": f"from-pub-date:{since.isoformat()}", "rows": rows,
                "select": SELECT, "mailto": self._mailto}

    async def search(self, terms: SearchTerms, limits: CollectionLimits, deadline: float) -> AsyncIterator[RawDocument]:
        phrases = [term for term, _language in english_first(terms)[:MAX_TERMS]]
        since = targeted_since(self._clock.now(), self._window_months, limits.published_since_year)
        rows = min(MAX_ROWS, math.ceil(limits.max_documents_per_source / max(1, len(phrases))))
        produced = 0
        for phrase in phrases:
            if self.deadline_reached(deadline):
                return
            payload = await self.fetch_json(API_URL, params=self._params(phrase, rows, since))
            for item in _items(payload):
                document = to_raw_document(item, phrase)
                if document is None or not within_since_year(document.published_at, limits.published_since_year):
                    continue
                produced += 1
                yield document
                if produced >= limits.max_documents_per_source:
                    return

    async def search_targeted(self, technologies: Sequence[str], per_term: int, since: date,
                              deadline: float) -> AsyncIterator[RawDocument]:
        for term in technologies:
            if self.deadline_reached(deadline):
                return
            payload = await self.fetch_json(API_URL, params=self._params(term, per_term, since))
            for item in _items(payload)[:per_term]:
                document = to_raw_document(item, term)
                if document is not None:
                    yield document


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Crossref: ответ без message")
    items = payload["message"].get("items", [])
    if not isinstance(items, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Crossref: message.items не список")
    return [item for item in items if isinstance(item, dict)]


def _first(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return " ".join(str(value or "").split())


def issued_date(item: dict[str, Any]) -> datetime | None:
    """Дата из `issued.date-parts` ([[год, месяц, день]], месяц и день необязательны)."""
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    if not parts or not isinstance(parts[0], int):
        return None
    year, month, day = (list(parts) + [1, 1])[:3]
    try:
        return datetime(year, month or 1, day or 1, tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def to_raw_document(item: dict[str, Any], term: str) -> RawDocument | None:
    title = _first(item.get("title"))
    doi = str(item.get("DOI") or "").strip()
    url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
    if not title or not url.startswith(("http://", "https://")):
        return None
    abstract = " ".join(html.unescape(_TAG_RE.sub(" ", str(item.get("abstract") or ""))).split())
    cited = item.get("is-referenced-by-count")
    return RawDocument(
        url=url,
        title=title[:512],
        text=" ".join(part for part in (title, abstract) if part)[:4000],
        matched_term=term,
        published_at=issued_date(item),
        language_code=None,
        doi=doi or None,
        citation_count=cited if isinstance(cited, int) else None,
        raw_meta={"container": _first(item.get("container-title")), "type": str(item.get("type") or "")},
    )
