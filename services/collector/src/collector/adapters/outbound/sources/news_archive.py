"""Адаптер локального архива новостей (этап 4.3): поиск по словам статей изданий и записей RSS без внешних запросов.

Архив наполняется заранее: `collector.tools.archive wayback` (адреса статей из Wayback Machine, раз в месяц)
и `collector.tools.archive rss` плюс каждый сбор (все записи лент). Фраза находит статью, если в ней есть все
значимые слова фразы; статьи — новые первыми, не старше окна `window_months`. Тип документа — по домену статьи
(`by_domain` в trust_rules), доверенность не выше MEDIUM.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.application.ports import HttpClient, NewsArchiveStore, RateLimiter
from collector.domain.archive import ArchiveEntry
from collector.domain.entities import RawDocument
from collector.domain.source_policy import SourcePolicy
from collector.domain.technology_mining import targeted_since
from collector.domain.text_tokens import phrase_words
from collector.domain.values import CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

MODEL_TECHNOLOGIES_IN_BROAD = 10


class NewsArchiveAdapter(BaseSourceAdapter):
    """Статьи архива по фразам темы (широкий проход) и по технологиям (прицельный)."""

    key = SourceKey.NEWS_ARCHIVE
    raw_meta_keys = frozenset({"publisher", "origin"})
    allowed_hosts = frozenset()
    supports_targeted = True
    policy = SourcePolicy(transient_retries=0)  # внешних запросов нет

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, store: NewsArchiveStore,
                 window_months: int = 24) -> None:
        super().__init__(http, limiter, clock)
        self._store = store
        self._window_months = window_months

    async def search(self, terms: SearchTerms, limits: CollectionLimits, deadline: float) -> AsyncIterator[RawDocument]:
        """Фразы темы и первые технологии модели; квота источника — поровну между фразами."""
        phrases = [term for term, _language in terms.pairs()]
        phrases += [t for t in terms.technologies[:MODEL_TECHNOLOGIES_IN_BROAD] if t not in phrases]
        since = targeted_since(self._clock.now(), self._window_months, limits.published_since_year)
        per_phrase = max(5, math.ceil(limits.max_documents_per_source / max(1, len(phrases))))
        produced, seen = 0, set()
        for phrase in phrases:
            words = phrase_words(phrase)
            if not words or self.deadline_reached(deadline):
                continue
            for entry in await self._store.search(words, since, per_phrase):
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                produced += 1
                yield to_raw_document(entry, phrase)
                if produced >= limits.max_documents_per_source:
                    return

    async def search_targeted(self, technologies: Sequence[str], per_term: int, since: date,
                              deadline: float) -> AsyncIterator[RawDocument]:
        """Этап 4.3: по `per_term` новых статей на технологию."""
        for term in technologies:
            words = phrase_words(term)
            if not words or self.deadline_reached(deadline):
                continue
            for entry in await self._store.search(words, since, per_term):
                yield to_raw_document(entry, term)


def to_raw_document(entry: ArchiveEntry, term: str) -> RawDocument:
    """Запись архива → сырой документ; текст — заголовок и анонс (у статей из Wayback — слова адреса)."""
    published = datetime(entry.published_on.year, entry.published_on.month, entry.published_on.day, tzinfo=UTC) \
        if entry.published_on else None
    return RawDocument(
        url=entry.url,
        title=entry.title,
        text=" ".join(part for part in (entry.title, entry.summary) if part)[:4000],
        matched_term=term,
        published_at=published,
        language_code=None,
        doi=None,
        raw_meta={"publisher": entry.publisher, "origin": entry.origin},
    )
