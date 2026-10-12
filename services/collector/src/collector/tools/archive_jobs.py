"""Фоновые задачи архивов (этап 5.0, аудит раунда 3 N15): общая реализация для команд `archive` и расписания collector.
Каждая задача возвращает строку итога; ошибки поднимаются к вызывающему.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from collector.adapters.outbound.news_archive.arxiv_oai import ArxivOaiHarvester
from collector.adapters.outbound.news_archive.enrich import ArchiveEnricher
from collector.adapters.outbound.news_archive.feeds import FeedDiscoverer, load_registry
from collector.adapters.outbound.news_archive.rss_archive import RssArchiver
from collector.adapters.outbound.news_archive.sitemaps import SitemapHarvester
from collector.adapters.outbound.news_archive.wayback import WaybackHarvester, load_publishers, months_between
from collector.domain.event_channel import EVENT_CUES
from collector.domain.text_tokens import tokens_of

JOBS = ("rss", "arxiv", "enrich", "sitemaps", "wayback", "discover-feeds")


async def run_job(name: str, settings, http, archive, corpus, clock,  # noqa: ANN001, PLR0913
                  report: Callable[[str], None] = print) -> str:
    """Одна фоновая задача архива по имени."""
    if name == "rss":
        return f"записей лент: {await RssArchiver(http, archive, settings.rss_feeds).run()}"
    if name == "arxiv":
        harvester = ArxivOaiHarvester(http, corpus, clock, settings.corpus_oai_pause_seconds, settings.corpus_since_year,
                                      report=report)
        states = [await harvester.harvest(s.strip()) for s in settings.corpus_arxiv_sets.split(",") if s.strip()]
        return "; ".join(f"{s.set_spec}: {s.status}, сохранено {s.kept}" for s in states)
    if name == "enrich":
        enricher = ArchiveEnricher(http, archive, clock, EVENT_CUES, settings.archive_enrich_pause_seconds, report=report)
        first = await enricher.run(settings.archive_schedule_enrich_batch, from_collections=True)
        second = await enricher.run(settings.archive_schedule_enrich_batch)
        return f"из сборов: {first}, общий архив: {second}"
    if name == "sitemaps":
        sources = load_registry(settings.source_registry_path, ["P0", "P1", "P2"])
        found, saved = await SitemapHarvester(http, archive, clock, settings.archive_discover_pause_seconds,
                                              report=report).run(sources)
        return f"ресурсов с картой сайта: {found}, статей: {saved}"
    if name == "wayback":
        months = months_between(settings.archive_wayback_from, datetime.now(UTC).strftime("%Y-%m"))
        refresh = months[-settings.archive_refresh_months:] if settings.archive_refresh_months > 0 else []
        total = await WaybackHarvester(http, archive, clock, settings.archive_wayback_pause_seconds, report=report).harvest(
            load_publishers(settings.news_publishers_path), months, refresh)
        return f"записей Wayback: {total}"
    if name == "discover-feeds":
        sources = load_registry(settings.source_registry_path, ["P0", "P1", "P2"])
        found, failed = await FeedDiscoverer(http, archive, clock, settings.archive_discover_pause_seconds,
                                             report=report).run(sources)
        return f"лент найдено у {found} ресурсов, без ленты {failed}"
    raise ValueError(f"неизвестная задача архива: {name}")


async def reindex(archive, corpus, batch: int = 5000, report: Callable[[str], None] = print) -> str:  # noqa: ANN001
    """Этап 5.0 (F23): слова записей — по заголовку и тексту до 1500 символов (прежде — 300)."""
    papers = news = 0
    after = ""
    while True:
        rows, after = await corpus.reindex_batch(after, batch)
        if not rows:
            break
        await corpus.set_tokens([(pid, tokens_of(f"{title} {(abstract or '')[:1500]}")) for pid, title, abstract in rows])
        papers += len(rows)
        report(f"корпус: {papers}")
    after = ""
    while True:
        rows, after = await archive.reindex_batch(after, batch)
        if not rows:
            break
        await archive.add_tokens([(url, tokens_of(f"{title} {(summary or '')[:1500]}")) for url, title, summary in rows])
        news += len(rows)
        report(f"архив новостей: {news}")
    return f"работ: {papers}, записей архива: {news}"
