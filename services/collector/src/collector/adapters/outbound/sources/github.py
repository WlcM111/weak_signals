"""Адаптер GitHub Search: репозитории как индикатор ранней инженерной активности."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from collector.adapters.outbound.sources.base import (
    PAGE_SIZE,
    BaseSourceAdapter,
    english_first,
    parse_datetime,
)
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://api.github.com/search/repositories"
MAX_PAGES = 3


class GithubAdapter(BaseSourceAdapter):
    """Поиск репозиториев, созданных не раньше заданного года, по убыванию звёзд."""

    key = SourceKey.GITHUB
    raw_meta_keys = frozenset({"topics", "language"})
    allowed_hosts = frozenset({"api.github.com"})

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, token: str = "") -> None:
        super().__init__(http, limiter, clock)
        self._token = token

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Репозитории по фразам запроса; токен повышает лимит источника, но не обязателен."""
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        produced = 0
        for term, _language in english_first(terms):
            for page in range(1, MAX_PAGES + 1):
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    return
                payload = await self.fetch_json(
                    API_URL,
                    params={
                        "q": f"{term} created:>{limits.published_since_year}-01-01",
                        "sort": "stars",
                        "order": "desc",
                        "per_page": PAGE_SIZE,
                        "page": page,
                    },
                    headers=headers,
                )
                items = _items(payload)
                for item in items:
                    document = _to_raw_document(item, term)
                    if document is None:
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
                if len(items) < PAGE_SIZE:
                    break


def _items(payload: Any) -> list[dict[str, Any]]:
    """Извлекает список репозиториев из ответа поиска."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "GitHub: в ответе нет списка items")
    return [item for item in payload["items"] if isinstance(item, dict)]


def _to_raw_document(item: dict[str, Any], term: str) -> RawDocument | None:
    """Преобразует репозиторий в сырой документ: текст = описание и темы, вовлечённость = звёзды."""
    url = item.get("html_url")
    title = item.get("full_name") or item.get("name")
    if not isinstance(url, str) or not isinstance(title, str) or not title:
        return None
    topics = [str(topic) for topic in item.get("topics", []) if isinstance(topic, str)][:10]
    description = str(item.get("description") or "")
    stars = item.get("stargazers_count")
    return RawDocument(
        url=url,
        title=title,
        text=" ".join(filter(None, [description, " ".join(topics)])),
        matched_term=term,
        published_at=parse_datetime(item.get("created_at")),
        engagement_count=int(stars) if isinstance(stars, int) else None,
        raw_meta={"topics": topics, "language": item.get("language") or ""},
    )
