"""Адаптер Wikipedia: индикатор зрелости технологии (`CheckEncyclopedia`).

Поиском документов не занимается: наличие статьи, просмотры за 30 дней и дата создания статьи.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from collector.adapters.outbound.sources.base import BaseSourceAdapter, parse_datetime
from collector.application.dto import EncyclopediaHit
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

SUMMARY_URL = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "{lang}.wikipedia/all-access/user/{title}/daily/{start}/{end}"
)
API_URL = "https://{lang}.wikipedia.org/w/api.php"
PAGEVIEWS_WINDOW_DAYS = 30
PAGEVIEWS_UNAVAILABLE = -1


class WikipediaProbe(BaseSourceAdapter):
    """Проверка статьи и её популярности; ошибки метрик не считаются отказом проверки."""

    key = SourceKey.WIKIPEDIA
    raw_meta_keys = frozenset()
    allowed_hosts = frozenset({"wikipedia.org", "wikimedia.org"})

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock) -> None:
        super().__init__(http, limiter, clock)
        self._log = get_logger("collector.source.wikipedia")

    def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Адаптер не выполняет поиск документов (§6.5 ТЗ: только индикатор зрелости)."""
        raise AdapterFailure(
            AdapterErrorCode.DISABLED.value, "wikipedia используется только через CheckEncyclopedia"
        )

    async def probe(self, title: str, language_code: str) -> EncyclopediaHit:
        """Наличие статьи, ссылка, просмотры за 30 дней и дата создания (если доступна)."""
        encoded = quote(title.strip().replace(" ", "_"), safe="")
        try:
            summary = await self.fetch_json(
                SUMMARY_URL.format(lang=language_code, title=encoded),
                headers={"Accept": "application/json"},
            )
        except AdapterFailure as failure:
            if failure.code == AdapterErrorCode.HTTP_4XX.value:
                return EncyclopediaHit(title=title, exists=False)
            raise
        page_url = _page_url(summary)
        return EncyclopediaHit(
            title=title,
            exists=True,
            page_url=page_url,
            pageviews_30d=await self._pageviews(encoded, language_code),
            created_at=await self._created_at(title, language_code),
        )

    async def _pageviews(self, encoded_title: str, language_code: str) -> int:
        """Сумма просмотров за 30 дней; при недоступности API возвращается -1 (§8 HANDOFF)."""
        end = self._clock.now()
        start = end - timedelta(days=PAGEVIEWS_WINDOW_DAYS)
        url = PAGEVIEWS_URL.format(
            lang=language_code,
            title=encoded_title,
            start=start.strftime("%Y%m%d"),
            end=end.strftime("%Y%m%d"),
        )
        try:
            payload = await self.fetch_json(url, headers={"Accept": "application/json"})
        except AdapterFailure as failure:
            self._log.debug("wikipedia.pageviews_unavailable", code=failure.code)
            return PAGEVIEWS_UNAVAILABLE
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return PAGEVIEWS_UNAVAILABLE
        return sum(
            int(item["views"])
            for item in payload["items"]
            if isinstance(item, dict) and isinstance(item.get("views"), int)
        )

    async def _created_at(self, title: str, language_code: str) -> Any:
        """Дата первой ревизии статьи; None, если API недоступен или ответ неполон."""
        try:
            payload = await self.fetch_json(
                API_URL.format(lang=language_code),
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvlimit": 1,
                    "rvdir": "newer",
                    "rvprop": "timestamp",
                    "titles": title,
                    "format": "json",
                    "formatversion": 2,
                },
                headers={"Accept": "application/json"},
            )
        except AdapterFailure as failure:
            self._log.debug("wikipedia.created_at_unavailable", code=failure.code)
            return None
        pages = payload.get("query", {}).get("pages", []) if isinstance(payload, dict) else []
        for page in pages if isinstance(pages, list) else []:
            revisions = page.get("revisions") if isinstance(page, dict) else None
            if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict):
                return parse_datetime(revisions[0].get("timestamp"))
        return None


def _page_url(summary: Any) -> str:
    """Каноническая ссылка на статью из ответа summary."""
    if not isinstance(summary, dict):
        return ""
    urls = summary.get("content_urls")
    if isinstance(urls, dict):
        desktop = urls.get("desktop")
        if isinstance(desktop, dict) and isinstance(desktop.get("page"), str):
            return str(desktop["page"])
    return ""
