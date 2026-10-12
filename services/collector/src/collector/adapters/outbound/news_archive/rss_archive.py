"""Архив RSS (этап 4.3): все записи лент сохраняются в архив новостей, а не только совпавшие с запросом.

Ленты хранят дни, поэтому без архива новости старше нескольких дней терялись для любых запросов. Записи
пишутся при каждом сборе (адаптер RSS) и командой `collector.tools.archive rss` — её стоит запускать ежедневно.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ws_common.identity import without_tracking

from collector.application.ports import HttpClient, NewsArchiveStore
from collector.domain.archive import ArchiveEntry
from collector.domain.errors import AdapterFailure
from collector.domain.text_tokens import tokens_of

_TAG_RE = re.compile(r"<[^>]+>")


def archive_entries(feed_entries: Iterable[object], fetched_at: datetime) -> list[ArchiveEntry]:
    """Записи ленты (`FeedEntry`) → записи архива; запись без заголовка или ссылки пропускается."""
    result = []
    for entry in feed_entries:
        title = " ".join(str(getattr(entry, "title", "") or "").split())
        link = str(getattr(entry, "link", "") or "").strip()
        parts = urlsplit(link)
        if not title or parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        summary = " ".join(html.unescape(_TAG_RE.sub(" ", str(getattr(entry, "summary", "") or ""))).split())[:2000]
        published = getattr(entry, "published_at", None)
        published_on = published.astimezone(UTC).date() if published else None  # этап 4.6 (P09): без даты — неизвестна
        tokens = tokens_of(f"{title} {summary[:1500]}")  # этап 5.0 (F23): было 300 символов анонса
        if tokens:
            result.append(ArchiveEntry(url=without_tracking(link),  # этап 5.0 (F13): ?id= не теряется
                                       publisher=parts.hostname.lower().removeprefix("www."), origin="rss",
                                       title=title[:512], published_on=published_on, captured_at=fetched_at,
                                       summary=summary, tokens=tuple(tokens),
                                       published_source="rss" if published_on else "unknown", text_status="rss"))
    return result


class RssArchiver:
    """Читает все ленты и сохраняет все записи в архив (запуск вне сбора)."""

    def __init__(self, http: HttpClient, store: NewsArchiveStore, feeds: Sequence[str],
                 report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._feeds, self._report = http, store, tuple(feeds), report

    async def run(self) -> int:
        from collector.adapters.outbound.sources.rss import FEED_ACCEPT, parse_feed

        total = 0
        feeds = list(dict.fromkeys(self._feeds))
        try:  # этап 4.6: и ленты ресурсов из реестра (discover-feeds)
            feeds += [feed for feed in await self._store.active_feeds() if feed not in feeds]
        except Exception as error:  # noqa: BLE001
            self._report(f"реестр лент недоступен: {error}")
        for feed in feeds:
            host = (urlsplit(feed).hostname or "").lower()
            try:
                body = await self._http.get_text(feed, allowed_hosts=frozenset({host}), headers=FEED_ACCEPT)
                entries = archive_entries(parse_feed(body, feed), datetime.now(UTC))
                saved = await self._store.save_entries(entries)
            except (AdapterFailure, Exception) as error:  # noqa: BLE001 — одна лента не останавливает остальные
                self._report(f"{feed}: ОТКАЗ {type(error).__name__}: {str(error)[:120]}")
                continue
            total += saved
            self._report(f"{feed}: записей {saved}")
        return total
