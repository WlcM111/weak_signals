"""Карты сайтов ресурсов реестра (этап 5.0, аудит раунда 3 N16, round2 F07): у многих newsroom компаний, лабораторий и
регуляторов нет RSS, но есть sitemap. Адреса карт берутся из robots.txt и типовых путей; индекс карт раскрывается до
MAX_CHILDREN самых свежих; записи статей (те же признаки, что у Wayback: слова в адресе, дата в пути) за последние
WINDOW_DAYS сохраняются в архив новостей с происхождением sitemap. Текст затем получает `archive enrich` с самого сайта.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from collector.adapters.outbound.news_archive.feeds import RegistrySource, _site_hosts
from collector.adapters.outbound.news_archive.wayback import article_entry
from collector.adapters.outbound.sources.xml_utils import parse_xml
from collector.application.ports import HttpClient, NewsArchiveStore
from ws_common.clock import Clock
from ws_common.identity import without_tracking

SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/news-sitemap.xml", "/sitemap-news.xml", "/post-sitemap.xml")
MAX_CHILDREN = 5
MAX_ENTRIES = 2000
WINDOW_DAYS = 120


def parse_sitemap(body: str) -> tuple[list[tuple[str, date | None, str, date | None]], list[tuple[str, date | None]]]:
    """Страницы (адрес, lastmod, заголовок news, дата публикации news) и вложенные карты (адрес, lastmod)."""
    root = parse_xml(body, what="sitemap")
    pages, children = [], []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in ("url", "sitemap"):
            continue
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in element.iter()}
        loc = values.get("loc", "")
        if not loc.startswith(("http://", "https://")):
            continue
        lastmod = _date(values.get("lastmod", ""))
        if tag == "sitemap":
            children.append((loc, lastmod))
        else:
            pages.append((loc, lastmod, values.get("title", ""), _date(values.get("publication_date", ""))))
    return pages, children


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


class SitemapHarvester:
    """Свежие статьи из карт сайтов ресурсов реестра."""

    def __init__(self, http: HttpClient, store: NewsArchiveStore, clock: Clock, pause_seconds: float = 1.0,
                 report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._clock, self._pause, self._report = http, store, clock, pause_seconds, report

    async def _get(self, url: str) -> str | None:
        await self._clock.sleep(self._pause)
        try:
            return await self._http.get_text(url, allowed_hosts=_site_hosts(url))
        except Exception:  # noqa: BLE001 — нет карты или недоступна: следующий адрес
            return None

    async def run(self, sources: Sequence[RegistrySource]) -> tuple[int, int]:
        """Возвращает (ресурсов с картой сайта, сохранено записей)."""
        since = self._clock.now().date() - timedelta(days=WINDOW_DAYS)
        with_map = saved = 0
        for source in sources:
            parts = urlsplit(source.url)
            root = f"{parts.scheme}://{parts.hostname}"
            robots = await self._get(root + "/robots.txt") or ""
            declared = [line.split(":", 1)[1].strip() for line in robots.splitlines()
                        if line.lower().startswith("sitemap:")]
            entries = []
            for address in [*declared[:3], *(root + path for path in SITEMAP_PATHS)]:
                body = await self._get(address)
                if not body:
                    continue
                try:
                    pages, children = parse_sitemap(body)
                except Exception:  # noqa: BLE001 — не XML
                    continue
                for child, _lastmod in sorted(children, key=lambda c: c[1] or date.min, reverse=True)[:MAX_CHILDREN]:
                    child_body = await self._get(child)
                    if child_body:
                        try:
                            pages += parse_sitemap(child_body)[0]
                        except Exception:  # noqa: BLE001
                            continue
                entries = [entry for entry in (self._entry(page, parts.hostname or "", since) for page in pages) if entry]
                if entries:
                    break
            if entries:
                with_map += 1
                saved += await self._store.save_entries(entries[:MAX_ENTRIES])
                self._report(f"{source.source_id:6} {root[:50]:50} карта сайта: {len(entries)} статей")
        return with_map, saved

    @staticmethod
    def _entry(page: tuple[str, date | None, str, date | None], host: str, since: date):  # noqa: ANN205
        loc, lastmod, news_title, news_date = page
        seen = news_date or lastmod
        if seen is not None and seen < since:
            return None
        stamp = (seen or datetime.now(UTC).date()).strftime("%Y%m%d") + "000000"
        entry = article_entry(without_tracking(loc), stamp, host.lower().removeprefix("www."))
        if entry is None:
            return None
        published, source = entry.published_on, entry.published_source
        if news_date is not None:
            published, source = news_date, "sitemap"
        return replace(entry, origin="sitemap", title=news_title or entry.title, published_on=published,
                       published_source=source if published else "unknown")
