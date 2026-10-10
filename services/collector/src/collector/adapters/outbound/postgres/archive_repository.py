"""PostgreSQL: архив новостей и корпус arXiv (этап 4.3). Поиск — по массиву слов (`tokens @>`, индекс GIN)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from collector.domain.archive import ArchiveEntry, CorpusHarvestState, CorpusPaper

_BATCH = 500

_SAVE_ENTRY = """
INSERT INTO news_archive (url, publisher, origin, published_on, captured_at, title, summary, tokens)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[])
ON CONFLICT (url) DO UPDATE SET
  title = CASE WHEN EXCLUDED.origin = 'rss' THEN EXCLUDED.title ELSE news_archive.title END,
  summary = CASE WHEN EXCLUDED.summary <> '' THEN EXCLUDED.summary ELSE news_archive.summary END,
  origin = CASE WHEN EXCLUDED.origin = 'rss' THEN 'rss' ELSE news_archive.origin END,
  published_on = COALESCE(news_archive.published_on, EXCLUDED.published_on),
  captured_at = COALESCE(news_archive.captured_at, EXCLUDED.captured_at),
  tokens = ARRAY(SELECT DISTINCT unnest(news_archive.tokens || EXCLUDED.tokens))
"""
_SEARCH = """
SELECT url, publisher, origin, published_on, captured_at, title, summary
FROM news_archive
WHERE tokens @> %s::text[] AND (published_on IS NULL OR published_on >= %s)
ORDER BY published_on DESC NULLS LAST, url
LIMIT %s
"""
_TITLES = """
SELECT title, published_on
FROM news_archive
WHERE tokens @> %s::text[] AND published_on >= %s
ORDER BY published_on DESC
LIMIT %s
"""
_PERIODS = "SELECT period, status FROM news_archive_harvest WHERE publisher = %s"
_MARK = """
INSERT INTO news_archive_harvest (publisher, period, status, rows, detail, finished_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (publisher, period) DO UPDATE SET
  status = EXCLUDED.status, rows = EXCLUDED.rows, detail = EXCLUDED.detail, finished_at = now()
"""
_ARCHIVE_STATS = """
SELECT publisher, origin, count(*) AS entries, min(published_on) AS first_on, max(published_on) AS last_on
FROM news_archive GROUP BY publisher, origin ORDER BY publisher, origin
"""
_SAVE_PAPER = """
INSERT INTO corpus_papers (paper_id, title, abstract, created_on, categories, tokens)
VALUES (%s, %s, %s, %s, %s::text[], %s::text[])
ON CONFLICT (paper_id) DO UPDATE SET
  title = EXCLUDED.title, abstract = EXCLUDED.abstract, categories = EXCLUDED.categories, tokens = EXCLUDED.tokens
"""
_PAPER_TITLES = """
SELECT title, created_on
FROM corpus_papers
WHERE tokens @> %s::text[] AND created_on >= %s
ORDER BY created_on DESC
LIMIT %s
"""
_HARVEST_STATE = "SELECT set_spec, resumption_token, records, kept, status FROM corpus_harvest WHERE set_spec = %s"
_SAVE_HARVEST = """
INSERT INTO corpus_harvest (set_spec, resumption_token, records, kept, status, detail, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (set_spec) DO UPDATE SET
  resumption_token = EXCLUDED.resumption_token, records = EXCLUDED.records, kept = EXCLUDED.kept,
  status = EXCLUDED.status, detail = EXCLUDED.detail, updated_at = now()
"""
_CORPUS_STATS = """
SELECT date_part('year', created_on)::int AS year, count(*) AS papers
FROM corpus_papers GROUP BY 1 ORDER BY 1
"""


class PostgresNewsArchive:
    """Реализация `NewsArchiveStore`."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def save_entries(self, entries: Sequence[ArchiveEntry]) -> int:
        rows = [(e.url, e.publisher, e.origin, e.published_on, e.captured_at, e.title[:512], e.summary[:2000],
                 list(e.tokens)) for e in entries if e.title and e.tokens]
        for start in range(0, len(rows), _BATCH):
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.executemany(_SAVE_ENTRY, rows[start : start + _BATCH])
        return len(rows)

    async def search(self, words: Sequence[str], since: date, limit: int) -> list[ArchiveEntry]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SEARCH, (list(words), since, limit))
            rows = await cur.fetchall()
        return [ArchiveEntry(url=r["url"], publisher=r["publisher"], origin=r["origin"], title=r["title"],
                             published_on=r["published_on"], captured_at=r["captured_at"], summary=r["summary"])
                for r in rows]

    async def titles_for(self, words: Sequence[str], since: date, limit: int) -> list[tuple[str, date | None]]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_TITLES, (list(words), since, limit))
            return [(row[0], row[1]) for row in await cur.fetchall()]

    async def harvested_periods(self, publisher: str) -> dict[str, str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PERIODS, (publisher,))
            return {row[0]: row[1] for row in await cur.fetchall()}

    async def mark_harvest(self, publisher: str, period: str, status: str, rows: int, detail: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_MARK, (publisher, period, status, rows, detail[:500]))

    async def stats(self) -> list[dict[str, object]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_ARCHIVE_STATS)
            return list(await cur.fetchall())


class PostgresCorpus:
    """Реализация `CorpusStore`."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def save_papers(self, papers: Sequence[CorpusPaper]) -> int:
        rows = [(p.paper_id, p.title[:1000], p.abstract[:1500], p.created_on, list(p.categories), list(p.tokens))
                for p in papers if p.title and p.tokens]
        for start in range(0, len(rows), _BATCH):
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.executemany(_SAVE_PAPER, rows[start : start + _BATCH])
        return len(rows)

    async def titles_for(self, words: Sequence[str], since: date, limit: int) -> list[tuple[str, date | None]]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PAPER_TITLES, (list(words), since, limit))
            return [(row[0], row[1]) for row in await cur.fetchall()]

    async def harvest_state(self, set_spec: str) -> CorpusHarvestState | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_HARVEST_STATE, (set_spec,))
            row = await cur.fetchone()
        if row is None:
            return None
        return CorpusHarvestState(row["set_spec"], row["resumption_token"], row["records"], row["kept"], row["status"])

    async def save_harvest_state(self, state: CorpusHarvestState, detail: str = "") -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SAVE_HARVEST, (state.set_spec, state.resumption_token, state.records, state.kept,
                                              state.status, detail[:500]))

    async def stats(self) -> list[dict[str, object]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_CORPUS_STATS)
            return list(await cur.fetchall())
