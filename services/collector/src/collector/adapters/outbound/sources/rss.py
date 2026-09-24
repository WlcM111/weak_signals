"""Адаптер RSS/Atom-лент отраслевых медиа (список лент — `WS_COLLECTOR_RSS_FEEDS`).

Разбор ведётся тем же безопасным парсером lxml, что и arXiv (единая защита от XXE и отсутствие
дополнительной зависимости); поддерживаются RSS 2.0 и Atom. Релевантность записи — доля токенов
фразы в заголовке и аннотации ≥ 0.5 (§6 HANDOFF).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

from collector.adapters.outbound.sources.base import BaseSourceAdapter, parse_datetime, within_since_year
from collector.adapters.outbound.sources.xml_utils import parse_xml, text_of
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.rules import term_coverage
from collector.domain.values import CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

RELEVANCE_THRESHOLD = 0.5
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class RssAdapter(BaseSourceAdapter):
    """Обходит настроенные ленты и отбирает записи, релевантные фразам запроса."""

    key = SourceKey.RSS
    raw_meta_keys = frozenset({"feed", "categories"})

    def __init__(
        self, http: HttpClient, limiter: RateLimiter, clock: Clock, feeds: Sequence[str]
    ) -> None:
        super().__init__(http, limiter, clock)
        if not feeds:
            raise ValueError("RssAdapter требует непустой список лент WS_COLLECTOR_RSS_FEEDS")
        self._feeds = tuple(feeds)
        self.allowed_hosts = frozenset(
            host for host in ((urlsplit(feed).hostname or "").lower() for feed in self._feeds) if host
        )
        self._log = get_logger("collector.source.rss")

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Записи лент, релевантные хотя бы одной фразе запроса."""
        produced = 0
        phrases = [term for term, _language in terms.pairs()]
        for feed_url in self._feeds:
            if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                return
            try:
                body = await self.fetch_text(feed_url)
            except AdapterFailure as failure:
                # отказ одной ленты не должен прекращать обход остальных
                self._log.warning("rss.feed_failed", feed=feed_url, code=failure.code)
                continue
            for entry in parse_feed(body, feed_url):
                matched = _best_term(entry, phrases)
                if matched is None:
                    continue
                document = RawDocument(
                    url=entry.link,
                    title=entry.title,
                    text=entry.summary,
                    matched_term=matched,
                    published_at=entry.published_at,
                    raw_meta={"feed": entry.feed_title, "categories": entry.categories[:10]},
                )
                if not within_since_year(document.published_at, limits.published_since_year):
                    continue
                produced += 1
                yield document
                if produced >= limits.max_documents_per_source:
                    return


class FeedEntry:
    """Запись ленты, приведённая к единому виду независимо от формата (RSS 2.0 / Atom)."""

    __slots__ = ("categories", "feed_title", "link", "published_at", "summary", "title")

    def __init__(
        self,
        title: str,
        link: str,
        summary: str,
        published_at: Any,
        feed_title: str,
        categories: list[str],
    ) -> None:
        self.title = title
        self.link = link
        self.summary = summary
        self.published_at = published_at
        self.feed_title = feed_title
        self.categories = categories


def parse_feed(body: str, feed_url: str) -> list[FeedEntry]:
    """Разбирает ленту RSS 2.0 или Atom в список записей."""
    root = parse_xml(body, what=f"лента {feed_url}")
    channel = root.find("channel")
    if channel is not None:
        feed_title = text_of(channel.find("title"))
        return [_rss_entry(item, feed_title) for item in channel.findall("item")]
    feed_title = text_of(root.find("atom:title", ATOM_NS))
    return [_atom_entry(entry, feed_title) for entry in root.findall("atom:entry", ATOM_NS)]


def _rss_entry(item: Any, feed_title: str) -> FeedEntry:
    """Запись RSS 2.0."""
    return FeedEntry(
        title=text_of(item.find("title")),
        link=text_of(item.find("link")),
        summary=text_of(item.find("description")),
        published_at=parse_datetime(text_of(item.find("pubDate")) or None),
        feed_title=feed_title,
        categories=[text_of(category) for category in item.findall("category") if text_of(category)],
    )


def _atom_entry(entry: Any, feed_title: str) -> FeedEntry:
    """Запись Atom."""
    link_element = entry.find("atom:link", ATOM_NS)
    link = link_element.get("href", "") if link_element is not None else ""
    summary = text_of(entry.find("atom:summary", ATOM_NS)) or text_of(entry.find("atom:content", ATOM_NS))
    published = text_of(entry.find("atom:published", ATOM_NS)) or text_of(entry.find("atom:updated", ATOM_NS))
    return FeedEntry(
        title=text_of(entry.find("atom:title", ATOM_NS)),
        link=link,
        summary=summary,
        published_at=parse_datetime(published or None),
        feed_title=feed_title,
        categories=[
            category.get("term", "")
            for category in entry.findall("atom:category", ATOM_NS)
            if category.get("term")
        ],
    )


def _best_term(entry: FeedEntry, phrases: Sequence[str]) -> str | None:
    """Наиболее релевантная фраза записи или None, если порог не достигнут."""
    if not entry.title or not entry.link.startswith(("http://", "https://")):
        return None
    haystack = f"{entry.title} {entry.summary}"
    best: tuple[float, str] | None = None
    for phrase in phrases:
        coverage = term_coverage(phrase, haystack)
        if coverage >= RELEVANCE_THRESHOLD and (best is None or coverage > best[0]):
            best = (coverage, phrase)
    return best[1] if best else None
