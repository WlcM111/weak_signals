"""Адаптер локального архива новостей (этапы 4.3–4.4): поиск по словам статей изданий и записей RSS без внешних запросов.

Этап 4.4: поиск идёт в основном по технологиям (общие фразы темы приносили самые свежие общие статьи), статьи
ранжируются по числу совпавших слов, а не по дате; короткая фраза (до двух слов) — все слова, длинная — все,
кроме одного. Слова ищутся в обеих формах числа (robot/robots).

Архив наполняется заранее: `collector.tools.archive wayback` (адреса статей из Wayback Machine, раз в месяц)
и `collector.tools.archive rss` плюс каждый сбор (все записи лент). Фраза находит статью, если в ней есть все
значимые слова фразы; статьи — новые первыми, не старше окна `window_months`. Тип документа — по домену статьи
(`by_domain` в trust_rules), доверенность не выше MEDIUM.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
from datetime import date

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.application.ports import HttpClient, NewsArchiveStore, RateLimiter
from collector.domain.archive import ArchiveEntry, archive_raw_document
from collector.domain.entities import RawDocument
from collector.domain.source_policy import SourcePolicy
from collector.domain.technology_filters import with_variants
from collector.domain.technology_mining import targeted_since
from collector.domain.text_tokens import phrase_words
from collector.domain.values import CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

BROAD_TECHNOLOGIES = 20  # широкий проход: технологии модели — основная часть фраз
BROAD_TOPIC_PHRASES = 2  # и не больше двух фраз темы


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
        """В основном технологии модели (до 20), плюс не больше двух фраз темы; квота источника — поровну."""
        topic = [term for term, _language in terms.pairs()][:BROAD_TOPIC_PHRASES]
        phrases = list(dict.fromkeys([*terms.technologies[:BROAD_TECHNOLOGIES], *topic]))
        since = targeted_since(self._clock.now(), self._window_months, limits.published_since_year)
        per_phrase = max(3, math.ceil(limits.max_documents_per_source / max(1, len(phrases))))
        produced, seen = 0, set()
        for phrase in phrases:
            words = phrase_words(phrase)
            if not words or self.deadline_reached(deadline):
                continue
            for entry in await self._store.search(with_variants(words), since, per_phrase, min_match(words)):
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                produced += 1
                yield to_raw_document(entry, phrase)
                if produced >= limits.max_documents_per_source:
                    return

    async def search_targeted(self, technologies: Sequence[str], per_term: int, since: date,
                              deadline: float, exclude: frozenset[str] = frozenset()) -> AsyncIterator[RawDocument]:
        """Этап 4.3: по `per_term` новых статей на технологию."""
        for term in technologies:
            words = phrase_words(term)
            if not words or self.deadline_reached(deadline):
                continue
            taken = 0  # этап 5.0 (F21): запрос с запасом, уже собранные статьи пропускаются, добор до per_term новых
            for entry in await self._store.search(with_variants(words), since, per_term * 3, min_match(words)):
                if entry.url in exclude:
                    continue
                yield to_raw_document(entry, term)
                taken += 1
                if taken >= per_term:
                    break


def min_match(words: Sequence[str]) -> int:
    """Сколько слов фразы должно совпасть: до двух слов — все, длиннее — все, кроме одного."""
    return len(words) if len(words) <= 2 else len(words) - 1


def to_raw_document(entry: ArchiveEntry, term: str) -> RawDocument:
    """Запись архива → сырой документ (общая функция домена, этап 4.5)."""
    return archive_raw_document(entry, term)
