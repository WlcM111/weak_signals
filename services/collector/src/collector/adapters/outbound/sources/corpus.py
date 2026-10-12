"""Адаптер локального корпуса arXiv (этап 4.6, аудит P13): научная выдача без внешних запросов.

Корпус (OAI-PMH, `collector.tools.archive arxiv`) прежде использовался только для словаря оценки возникновения, а
работы в выдачу сбора не попадали; живой адаптер arXiv ограничен двумя фразами и одной страницей. Теперь корпус —
отдельный источник: фраза находит работу по словам заголовка и начала аннотации (короткая — все слова, длинная —
все, кроме одного), порядок — по числу совпавших слов, затем новее. Глубина не ограничена страницами внешнего API.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.adapters.outbound.sources.news_archive import min_match
from collector.application.ports import CorpusStore, HttpClient, RateLimiter
from collector.domain.archive import CorpusPaper
from collector.domain.entities import RawDocument
from collector.domain.source_policy import SourcePolicy
from collector.domain.technology_filters import with_variants
from collector.domain.technology_mining import targeted_since
from collector.domain.text_tokens import phrase_words
from collector.domain.values import CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

BROAD_TECHNOLOGIES = 10


class CorpusAdapter(BaseSourceAdapter):
    """Работы локального корпуса arXiv по фразам темы и по технологиям."""

    key = SourceKey.CORPUS
    raw_meta_keys = frozenset({"categories"})
    allowed_hosts = frozenset()
    supports_targeted = True
    policy = SourcePolicy(transient_retries=0)  # внешних запросов нет

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, store: CorpusStore,
                 window_months: int = 24) -> None:
        super().__init__(http, limiter, clock)
        self._store = store
        self._window_months = window_months

    async def search(self, terms: SearchTerms, limits: CollectionLimits, deadline: float) -> AsyncIterator[RawDocument]:
        phrases = list(dict.fromkeys([*terms.en, *terms.technologies[:BROAD_TECHNOLOGIES]]))
        since = targeted_since(self._clock.now(), self._window_months, limits.published_since_year)
        per_phrase = max(3, math.ceil(limits.max_documents_per_source / max(1, len(phrases))))
        produced, seen = 0, set()
        for phrase in phrases:
            words = phrase_words(phrase)
            if not words or self.deadline_reached(deadline):
                continue
            for paper in await self._store.search(with_variants(words), since, per_phrase, min_match(words)):
                if paper.paper_id in seen:
                    continue
                seen.add(paper.paper_id)
                produced += 1
                yield to_raw_document(paper, phrase)
                if produced >= limits.max_documents_per_source:
                    return

    async def search_targeted(self, technologies: Sequence[str], per_term: int, since: date,
                              deadline: float, exclude: frozenset[str] = frozenset()) -> AsyncIterator[RawDocument]:
        for term in technologies:
            words = phrase_words(term)
            if not words or self.deadline_reached(deadline):
                continue
            taken = 0  # этап 5.0 (F21): запрос с запасом, уже собранные работы пропускаются, добор до per_term новых
            for paper in await self._store.search(with_variants(words), since, per_term * 3, min_match(words)):
                if f"https://arxiv.org/abs/{paper.paper_id}" in exclude:
                    continue
                yield to_raw_document(paper, term)
                taken += 1
                if taken >= per_term:
                    break


def to_raw_document(paper: CorpusPaper, term: str) -> RawDocument:
    return RawDocument(
        url=f"https://arxiv.org/abs/{paper.paper_id}",
        title=paper.title[:512],
        text=" ".join(part for part in (paper.title, paper.abstract) if part)[:4000],
        matched_term=term,
        published_at=datetime(paper.created_on.year, paper.created_on.month, paper.created_on.day, tzinfo=UTC),
        language_code="en",
        doi=None,
        raw_meta={"categories": list(paper.categories)[:5]},
    )
