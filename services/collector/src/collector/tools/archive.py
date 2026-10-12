"""Команды локальных архивов (этап 4.3). Запуск в контейнере collector:

    python -m collector.tools.archive wayback [--from 2025-01] [--to 2026-10] [--publisher techcrunch.com]
    python -m collector.tools.archive rss
    python -m collector.tools.archive arxiv [--sets cs,eess,q-fin] [--since-year 2021] [--restart]
    python -m collector.tools.archive status

wayback — перечисляет статьи изданий (`config/news_publishers.yaml`) по месяцам через Wayback Machine CDX API:
первичная загрузка — с `--from`, повторный запуск продолжает с неперечисленных месяцев и обновляет последние
`WS_ARCHIVE_REFRESH_MONTHS` месяцев (запускать раз в месяц). rss — все записи всех лент (запускать ежедневно).
arxiv — корпус метаданных arXiv через OAI-PMH, возобновляемо. status — сводка архива и корпуса.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from collector.adapters.outbound.http_client import HttpxClient
from collector.adapters.outbound.news_archive.arxiv_oai import ArxivOaiHarvester
from collector.adapters.outbound.news_archive.rss_archive import RssArchiver
from collector.adapters.outbound.news_archive.wayback import WaybackHarvester, load_publishers, months_between
from collector.adapters.outbound.postgres.archive_repository import PostgresCorpus, PostgresNewsArchive
from collector.config import CollectorSettings
from ws_common.clock import SystemClock
from ws_common.db import build_pool


def _parser(settings: CollectorSettings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector.tools.archive", description="Локальные архивы collector")
    commands = parser.add_subparsers(dest="command", required=True)
    wayback = commands.add_parser("wayback", help="статьи изданий из Wayback Machine")
    wayback.add_argument("--from", dest="first", default=settings.archive_wayback_from)
    wayback.add_argument("--to", dest="last", default=datetime.now(UTC).strftime("%Y-%m"))
    wayback.add_argument("--publisher", action="append", default=[])
    wayback.add_argument("--refresh", type=int, default=settings.archive_refresh_months)
    commands.add_parser("rss", help="все записи RSS-лент")
    arxiv = commands.add_parser("arxiv", help="корпус arXiv через OAI-PMH")
    arxiv.add_argument("--sets", default=settings.corpus_arxiv_sets)
    arxiv.add_argument("--since-year", type=int, default=settings.corpus_since_year)
    arxiv.add_argument("--restart", action="store_true")
    enrich = commands.add_parser("enrich", help="текст статей архива из архивных копий страниц (этап 4.6)")
    enrich.add_argument("--limit", type=int, default=2000)
    enrich.add_argument("--from-collections", action="store_true",
                        help="сначала статьи архива, попавшие в сборы (этап 4.7)")
    discover = commands.add_parser("discover-feeds", help="ленты RSS/Atom ресурсов реестра (этап 4.6)")
    discover.add_argument("--priority", default="P1,P2")
    discover.add_argument("--limit", type=int, default=0)
    commands.add_parser("sitemaps", help="свежие статьи из карт сайтов ресурсов реестра (этап 5.0)")
    commands.add_parser("reindex", help="пересчёт слов корпуса и архива по тексту до 1500 символов (этап 5.0)")
    commands.add_parser("status", help="сводка архива и корпуса")
    return parser


async def _run(argv: list[str]) -> int:
    settings = CollectorSettings()
    args = _parser(settings).parse_args(argv)
    pool = build_pool(settings.pg_dsn, min_size=1, max_size=3,
                      statement_timeout_ms=max(settings.pg_statement_timeout_ms, 600_000),
                      application_name="collector-archive")
    await pool.open(wait=True, timeout=30)
    http = HttpxClient(contact_email=settings.contact_email, read_timeout_seconds=180,
                       max_bytes=max(settings.http_max_bytes, 64 * 1024 * 1024), retries=0)
    clock = SystemClock()
    archive, corpus = PostgresNewsArchive(pool), PostgresCorpus(pool)
    try:
        if args.command == "wayback":
            publishers = load_publishers(settings.news_publishers_path)
            if args.publisher:
                publishers = [p for p in publishers if p.host in {h.lower() for h in args.publisher}]
            months = months_between(args.first, args.last)
            refresh = months[-args.refresh:] if args.refresh > 0 else []
            total = await WaybackHarvester(http, archive, clock, settings.archive_wayback_pause_seconds).harvest(
                publishers, months, refresh)
            print(f"ИТОГО записей сохранено: {total}")
        elif args.command == "rss":
            total = await RssArchiver(http, archive, settings.rss_feeds).run()
            print(f"ИТОГО записей лент сохранено: {total}")
        elif args.command == "arxiv":
            harvester = ArxivOaiHarvester(http, corpus, clock, settings.corpus_oai_pause_seconds, args.since_year)
            for set_spec in [s.strip() for s in args.sets.split(",") if s.strip()]:
                state = await harvester.harvest(set_spec, restart=args.restart)
                print(f"{set_spec}: статус {state.status}, записей {state.records}, сохранено {state.kept}")
        elif args.command == "enrich":
            from collector.adapters.outbound.news_archive.enrich import ArchiveEnricher
            from collector.domain.event_channel import EVENT_CUES

            done, failed = await ArchiveEnricher(http, archive, clock, EVENT_CUES,
                                                 settings.archive_enrich_pause_seconds).run(args.limit,
                                                                                            args.from_collections)
            print(f"ИТОГО: с текстом {done}, без текста {failed}")
        elif args.command == "discover-feeds":
            from collector.adapters.outbound.news_archive.feeds import FeedDiscoverer, load_registry

            sources = load_registry(settings.source_registry_path, [p.strip() for p in args.priority.split(",") if p.strip()])
            sources = sources[: args.limit] if args.limit > 0 else sources
            found, failed = await FeedDiscoverer(http, archive, clock, settings.archive_discover_pause_seconds).run(sources)
            print(f"ИТОГО ресурсов: {len(sources)}; лент найдено {found}, не найдено {failed}")
        elif args.command == "sitemaps":
            from collector.tools.archive_jobs import run_job

            print("ИТОГО: " + await run_job("sitemaps", settings, http, archive, corpus, clock))
        elif args.command == "reindex":
            from collector.tools.archive_jobs import reindex

            print("ИТОГО: " + await reindex(archive, corpus))
        else:
            print("== Архив новостей (издание, происхождение, записей, с, по)")
            for row in await archive.stats():
                print(f"  {row['publisher']:32} {row['origin']:8} {row['entries']:7} {row['first_on']} {row['last_on']}")
            print("== Корпус arXiv (год, работ)")
            for row in await corpus.stats():
                print(f"  {row['year']} {row['papers']}")
            for set_spec in [s.strip() for s in settings.corpus_arxiv_sets.split(",") if s.strip()]:
                state = await corpus.harvest_state(set_spec)
                print(f"  набор {set_spec}: " + (f"{state.status}, записей {state.records}, сохранено {state.kept}"
                                                  if state else "не выгружался"))
    finally:
        await http.aclose()
        await pool.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(sys.argv[1:])))


if __name__ == "__main__":
    main()
