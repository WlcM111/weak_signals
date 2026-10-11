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
from collector.adapters.outbound.news_archive.rss_archive import archive_entries
from collector.application.ports import HttpClient, NewsArchiveStore, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.rules import term_coverage
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock
from ws_common.logging import get_logger

RELEVANCE_THRESHOLD = 0.5
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


# Этап 4.2: явный Accept ленты. PR Newswire на запрос без него отвечает 301 на адрес со слешем и затем 404,
# с этим заголовком — сразу 200 и валидная лента (диагностика API 07.10.2026, rss_31).
FEED_ACCEPT = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"}
RSS1_NS = {"rss1": "http://purl.org/rss/1.0/", "dc": "http://purl.org/dc/elements/1.1/"}


class RssAdapter(BaseSourceAdapter):
    """Обходит настроенные ленты и отбирает записи, релевантные фразам запроса."""

    key = SourceKey.RSS
    raw_meta_keys = frozenset({"feed", "categories"})

    def __init__(
        self, http: HttpClient, limiter: RateLimiter, clock: Clock, feeds: Sequence[str],
        archive: NewsArchiveStore | None = None,
    ) -> None:
        super().__init__(http, limiter, clock)
        self._archive = archive  # этап 4.3: все записи лент — в архив новостей
        self._rotation = 0  # этап 4.6 (P21): сдвиг начала обхода лент между сборами
        if not feeds:
            raise ValueError("RssAdapter требует непустой список лент WS_COLLECTOR_RSS_FEEDS")
        self._feeds = tuple(feeds)
        self.allowed_hosts = frozenset(
            host for host in ((urlsplit(feed).hostname or "").lower() for feed in self._feeds) if host
        )
        self._log = get_logger("collector.source.rss")

    async def _archive_all(self, entries: list[FeedEntry]) -> None:
        """Все записи ленты — в архив новостей; сбой архива не мешает сбору."""
        if self._archive is None or not entries:
            return
        try:
            await self._archive.save_entries(archive_entries(entries, self._clock.now()))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("rss.archive_failed", error=str(exc)[:200])

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Записи лент, релевантные хотя бы одной фразе запроса или технологии темы (этап 4.2).

        Ленты читаются один раз, поэтому записи сверяются и с технологиями от модели — без новых запросов.
        Отказ одной ленты не прекращает обход; если отказали все ленты, источник сообщает отказ, а не пустую выдачу.
        """
        produced = 0
        phrases = [term for term, _language in terms.pairs()] + [t for t in terms.technologies if t not in terms.en]
        failures: list[tuple[str, AdapterFailure]] = []
        attempted = 0
        # Этап 4.6 (аудит P21): прежде ленты читались по порядку до заполнения квоты — поздние не читались и не
        # попадали в архив. Теперь начало обхода чередуется между сборами, у каждой ленты своя доля квоты, а после
        # заполнения квоты ленты дочитываются ради архива (до дедлайна), но документы больше не отдаются.
        feeds = list(self._feeds)
        if feeds:
            shift = self._rotation % len(feeds)
            feeds = feeds[shift:] + feeds[:shift]
            self._rotation += 1
        per_feed = max(3, -(-limits.max_documents_per_source // max(1, len(feeds))))
        for feed_url in feeds:
            if self.deadline_reached(deadline):
                break
            attempted += 1
            try:
                body = await self.fetch_text(feed_url, headers=FEED_ACCEPT)
                entries = list(parse_feed(body, feed_url))
                await self._archive_all(entries)
            except AdapterFailure as failure:
                # отказ или неразборчивый ответ одной ленты не прекращает обход остальных (стенд 29.09: пустой
                # ответ 202 одной ленты обрывал весь адаптер с PARSE_ERROR, и следующие ленты не читались)
                self._log.warning("rss.feed_failed", feed=feed_url, code=failure.code, detail=failure.message[:200])
                failures.append((feed_url, failure))
                continue
            taken = 0
            for entry in entries:
                if produced >= limits.max_documents_per_source or taken >= per_feed:
                    break
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
                taken += 1
                yield document
        if failures and len(failures) == attempted:
            first_url, first = failures[0]
            raise AdapterFailure(first.code, f"все {attempted} лент недоступны; первая {first_url}: {first.message}")


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
    """Разбирает ленту RSS 2.0, RSS 1.0 (RDF) или Atom; другой корневой элемент — отказ PARSE_ERROR.

    Этап 4.2: прежде HTML или иной XML без ленты тихо превращался в ноль записей (аудит API 07.10.2026, п. 2.6).
    """
    root = parse_xml(body, what=f"лента {feed_url}")
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, f"лента {feed_url}: RSS без channel")
        feed_title = text_of(channel.find("title"))
        return [_rss_entry(item, feed_title) for item in channel.findall("item")]
    if tag == "rdf":
        channel = root.find("rss1:channel", RSS1_NS)
        feed_title = text_of(channel.find("rss1:title", RSS1_NS)) if channel is not None else ""
        return [_rdf_entry(item, feed_title) for item in root.findall("rss1:item", RSS1_NS)]
    if tag == "feed":
        feed_title = text_of(root.find("atom:title", ATOM_NS))
        return [_atom_entry(entry, feed_title) for entry in root.findall("atom:entry", ATOM_NS)]
    raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, f"лента {feed_url}: не RSS и не Atom (корень <{tag}>)")


def _rdf_entry(item: Any, feed_title: str) -> FeedEntry:
    """Запись RSS 1.0 (RDF): заголовок, ссылка, описание и дата Dublin Core."""
    return FeedEntry(
        title=text_of(item.find("rss1:title", RSS1_NS)),
        link=text_of(item.find("rss1:link", RSS1_NS)),
        summary=text_of(item.find("rss1:description", RSS1_NS)),
        published_at=parse_datetime(text_of(item.find("dc:date", RSS1_NS)) or None),
        feed_title=feed_title,
        categories=[text_of(subject) for subject in item.findall("dc:subject", RSS1_NS) if text_of(subject)],
    )


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
