"""Адаптер Hugging Face Hub API (этап 4.3): модели и наборы данных по названию.

`GET https://huggingface.co/api/models?search=…&limit=N&sort=likes&direction=-1` и то же для `/api/datasets` —
ключ не нужен. Полезен для тем edge-вычислений и инфраструктуры ИИ: новые модели и наборы появляются там раньше
статей. Документ — карточка репозитория: название, метки, дата создания, число отметок «нравится».
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any

from collector.adapters.outbound.sources.base import BaseSourceAdapter, english_first, within_since_year
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

MODELS_URL = "https://huggingface.co/api/models"
DATASETS_URL = "https://huggingface.co/api/datasets"
MAX_TERMS = 2
MAX_LIMIT = 50


class HuggingFaceAdapter(BaseSourceAdapter):
    """Модели и наборы Hugging Face по фразам темы и по технологиям."""

    key = SourceKey.HUGGINGFACE
    raw_meta_keys = frozenset({"kind", "likes", "downloads"})
    allowed_hosts = frozenset({"huggingface.co"})
    supports_targeted = True

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock) -> None:
        super().__init__(http, limiter, clock)

    async def _query(self, url: str, query: str, limit: int) -> list[dict[str, Any]]:
        payload = await self.fetch_json(url, params={"search": query, "limit": limit, "sort": "likes", "direction": -1})
        if not isinstance(payload, list):
            raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Hugging Face: ответ не список")
        return [item for item in payload if isinstance(item, dict)]

    async def search(self, terms: SearchTerms, limits: CollectionLimits, deadline: float) -> AsyncIterator[RawDocument]:
        phrases = [term for term, _language in english_first(terms)[:MAX_TERMS]]
        limit = min(MAX_LIMIT, math.ceil(limits.max_documents_per_source / max(1, 2 * len(phrases))))
        produced = 0
        for phrase in phrases:
            for url, kind in ((MODELS_URL, "model"), (DATASETS_URL, "dataset")):
                if self.deadline_reached(deadline):
                    return
                for item in await self._query(url, phrase, limit):
                    document = to_raw_document(item, phrase, kind)
                    if document is None or not within_since_year(document.published_at, limits.published_since_year):
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return

    async def search_targeted(self, technologies: Sequence[str], per_term: int, since: date,
                              deadline: float) -> AsyncIterator[RawDocument]:
        start = datetime(since.year, since.month, since.day, tzinfo=UTC)
        for term in technologies:
            if self.deadline_reached(deadline):
                return
            for item in await self._query(MODELS_URL, term, per_term):
                document = to_raw_document(item, term, "model")
                if document is not None and (document.published_at is None or document.published_at >= start):
                    yield document


def to_raw_document(item: dict[str, Any], term: str, kind: str) -> RawDocument | None:
    repo = str(item.get("id") or item.get("modelId") or "").strip()
    if not repo or "/" not in repo:
        return None
    tags = [str(tag) for tag in item.get("tags") or [] if isinstance(tag, str) and ":" not in tag][:20]
    created = item.get("createdAt")
    try:
        published = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(UTC) if created else None
    except ValueError:
        published = None
    likes = item.get("likes") if isinstance(item.get("likes"), int) else None
    base = "https://huggingface.co/" + ("datasets/" if kind == "dataset" else "")
    return RawDocument(
        url=base + repo,
        title=repo.replace("/", " / ")[:512],
        text=" ".join([repo.split("/", 1)[1].replace("-", " "), *tags])[:4000],
        matched_term=term,
        published_at=published,
        language_code=None,
        doi=None,
        engagement_count=likes,
        raw_meta={"kind": kind, "likes": likes or 0, "downloads": int(item.get("downloads") or 0)},
    )
