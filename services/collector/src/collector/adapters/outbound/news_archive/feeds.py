"""Поиск лент RSS/Atom ресурсов реестра (этап 4.6, документ «02_weak_signals_sources»).

Для ресурсов и доменов из `config/source_registry.yaml` со страницы ресурса (или корня домена) берутся ссылки
`<link rel="alternate" type="application/rss+xml|atom+xml">`, а если их нет — типовые адреса лент того же сайта.
Каждая лента проверяется разбором: в реестр со статусом active попадает только лента с записями. Адреса не
придумываются заранее — их подтверждает ответ сайта из сети стенда.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

from collector.application.ports import HttpClient, NewsArchiveStore
from ws_common.clock import Clock

_LINK = re.compile(r"<link\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:-]*)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.S)
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")
COMMON_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/news/feed", "/blog/feed")
MAX_FEEDS_PER_SOURCE = 3  # этап 5.0 (F08): несколько тематических лент одного издателя


@dataclass(frozen=True)
class RegistrySource:
    source_id: str
    url: str
    priority: str = ""


def load_registry(path: Path, priorities: Sequence[str] = ()) -> list[RegistrySource]:
    """Ресурсы и домены реестра, для которых разрешён поиск лент (и выбранных приоритетов ресурсов)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = []
    for item in data.get("resources") or []:
        if item.get("discover", True) and item.get("url") and (not priorities or item.get("priority") in priorities):
            sources.append(RegistrySource(item["id"], item["url"], item.get("priority", "")))
    for item in data.get("domains") or []:
        if item.get("discover", True) and item.get("url"):
            sources.append(RegistrySource(item["id"], item["url"], "domain"))
    return sources


def feed_links(page: str, base_url: str) -> list[str]:
    """Ссылки на ленты из `<link rel="alternate">` страницы, абсолютные."""
    found = []
    for tag in _LINK.findall(page or ""):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        if "alternate" in (attrs.get("rel") or "").lower() and (attrs.get("type") or "").lower() in FEED_TYPES and attrs.get("href"):
            url = urljoin(base_url, attrs["href"].strip())
            if url.startswith(("http://", "https://")) and url not in found:
                found.append(url)
    return found


def _site_hosts(url: str) -> frozenset[str]:
    host = (urlsplit(url).hostname or "").lower()
    bare = host.removeprefix("www.")
    return frozenset({host, bare, f"www.{bare}"})


class FeedDiscoverer:
    """Находит и проверяет ленты ресурсов, результат — в реестр лент."""

    def __init__(self, http: HttpClient, store: NewsArchiveStore, clock: Clock, pause_seconds: float = 1.0,
                 report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._clock, self._pause, self._report = http, store, clock, pause_seconds, report

    async def run(self, sources: Sequence[RegistrySource]) -> tuple[int, int]:
        from collector.adapters.outbound.sources.rss import FEED_ACCEPT, parse_feed

        found = failed = 0
        for source in sources:
            hosts = _site_hosts(source.url)
            candidates: list[str] = []
            try:
                await self._clock.sleep(self._pause)
                page = await self._http.get_text(source.url, allowed_hosts=hosts)
                candidates = feed_links(page, source.url)
            except Exception:  # noqa: BLE001 — недоступная страница: проверяются типовые адреса лент
                page = ""
            root = f"{urlsplit(source.url).scheme}://{urlsplit(source.url).hostname}"
            candidates += [root + path for path in COMMON_PATHS if root + path not in candidates]
            accepted, reasons = [], []
            # Этап 5.0 (аудит round2 F08): проверяются все объявленные и все запасные адреса (прежде — первые 6 из
            # объявленных + 9 запасных), принимается до MAX_FEEDS_PER_SOURCE тематических лент, причины отказа сохраняются.
            for feed in candidates:
                if len(accepted) >= MAX_FEEDS_PER_SOURCE:
                    break
                feed_hosts = hosts | _site_hosts(feed)
                try:
                    await self._clock.sleep(self._pause)
                    body = await self._http.get_text(feed, allowed_hosts=feed_hosts, headers=FEED_ACCEPT)
                    entries = parse_feed(body, feed)
                except Exception as error:  # noqa: BLE001 — не лента или недоступна: следующий кандидат
                    reasons.append(f"{feed}: {type(error).__name__}")
                    continue
                if entries:
                    accepted.append((feed, len(entries), entries[0].feed_title or ""))
                else:
                    reasons.append(f"{feed}: лента без записей")
            for feed, entries_count, title in accepted:
                await self._store.save_feed(feed, source.source_id, source.url, title, "active", entries_count, "")
                self._report(f"{source.source_id:6} {source.url[:60]:60} лента {feed} ({entries_count} записей)")
            if accepted:
                found += 1
            else:
                await self._store.save_feed(f"{source.url}#no-feed", source.source_id, source.url, "", "failed", 0,
                                            "; ".join(reasons)[-480:] or "лента не найдена")
                failed += 1
        return found, failed
