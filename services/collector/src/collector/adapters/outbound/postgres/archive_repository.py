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

# Этап 4.6 (аудит P09): более достоверная дата (RSS, страница) заменяет дату из адреса или неизвестную;
# дата сохранения в архиве публикацией не считается. Заголовок и анонс RSS заменяют слова адреса.
_BETTER_DATE = """EXCLUDED.published_on IS NOT NULL AND (news_archive.published_on IS NULL
    OR (EXCLUDED.published_source IN ('rss', 'page') AND news_archive.published_source NOT IN ('rss', 'page')))"""
_SAVE_ENTRY = f"""
INSERT INTO news_archive (url, publisher, origin, published_on, captured_at, title, summary, tokens, published_source,
                          text_status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s)
ON CONFLICT (url) DO UPDATE SET
  title = CASE WHEN news_archive.text_status = 'slug' AND EXCLUDED.text_status <> 'slug' THEN EXCLUDED.title
               ELSE news_archive.title END,
  summary = CASE WHEN length(EXCLUDED.summary) > length(news_archive.summary) THEN EXCLUDED.summary
                 ELSE news_archive.summary END,
  origin = CASE WHEN EXCLUDED.origin = 'rss' THEN 'rss' ELSE news_archive.origin END,
  text_status = CASE WHEN news_archive.text_status = 'slug' THEN EXCLUDED.text_status ELSE news_archive.text_status END,
  published_on = CASE WHEN {_BETTER_DATE} THEN EXCLUDED.published_on ELSE news_archive.published_on END,
  published_source = CASE WHEN {_BETTER_DATE} THEN EXCLUDED.published_source ELSE news_archive.published_source END,
  captured_at = COALESCE(news_archive.captured_at, EXCLUDED.captured_at),
  tokens = ARRAY(SELECT DISTINCT unnest(news_archive.tokens || EXCLUDED.tokens))
"""
# Этап 4.4: число совпавших слов — пересечение массива слов записи с запросом; индекс GIN отбирает записи хотя бы
# с одним словом (`&&`), порядок — по числу совпавших слов, затем по дате.
_MATCHED = "cardinality(ARRAY(SELECT unnest(tokens) INTERSECT SELECT unnest(%s::text[])))"
_SEARCH = f"""
SELECT url, publisher, origin, published_on, captured_at, title, summary, published_source, text_status
FROM news_archive
WHERE tokens && %s::text[] AND {_MATCHED} >= %s AND COALESCE(published_on, captured_at::date, %s) >= %s
ORDER BY {_MATCHED} DESC, published_on DESC NULLS LAST, url
LIMIT %s
"""
_TITLES = f"""
SELECT title, published_on
FROM news_archive
WHERE tokens && %s::text[] AND {_MATCHED} >= %s AND published_on >= %s AND published_on < %s
  AND (%s::text IS NULL OR origin = %s::text)
  AND (%s::text[] IS NULL OR tokens && %s::text[])
ORDER BY md5(url)
LIMIT %s
"""
# Этап 4.7.1: запрос канала событий только отбирает кандидатов (событие, не меньше двух слов словаря, окно дат) и
# возвращает совпавшие слова — без сортировки и без тяжёлых вычислений на строку. Ранжирование по редкости
# совпавших основ среди ВСЕХ кандидатов делает rank_events (тот же расчёт, что в диагностике 11.10.2026: 0,5–1,2 с
# на 4–19 тыс. кандидатов). Ранжирование в CTE (4.7) не укладывалось в таймаут запроса.
_EVENTS = """
SELECT url, publisher, origin, published_on, captured_at, title, summary, published_source, text_status,
       ARRAY(SELECT unnest(tokens) INTERSECT SELECT unnest(%s::text[])) AS matched
FROM news_archive
WHERE tokens && %s::text[] AND tokens && %s::text[]
  AND cardinality(ARRAY(SELECT unnest(tokens) INTERSECT SELECT unnest(%s::text[]))) >= %s
  AND COALESCE(published_on, captured_at::date) >= %s
LIMIT %s
"""
_FROM_COLLECTIONS = """
SELECT a.url, a.publisher, a.origin, a.published_on, a.captured_at, a.title, a.summary, a.published_source,
       a.text_status, max(c.created_at) AS last_seen
FROM news_archive a
JOIN documents d ON d.url = a.url
JOIN collection_documents cd ON cd.document_id = d.document_id
JOIN collections c ON c.collection_id = cd.collection_id
WHERE a.text_status = 'slug' AND a.origin = 'wayback'
GROUP BY a.url, a.publisher, a.origin, a.published_on, a.captured_at, a.title, a.summary, a.published_source,
         a.text_status
ORDER BY last_seen DESC
LIMIT %s
"""
_PERIODS = "SELECT period, status, resume FROM news_archive_harvest WHERE publisher = %s"
_MARK = """
INSERT INTO news_archive_harvest (publisher, period, status, rows, detail, resume, finished_at)
VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (publisher, period) DO UPDATE SET
  status = EXCLUDED.status, rows = news_archive_harvest.rows + EXCLUDED.rows, detail = EXCLUDED.detail,
  resume = EXCLUDED.resume, finished_at = now()
"""
_TO_ENRICH = """
SELECT url, publisher, origin, published_on, captured_at, title, summary, published_source, text_status
FROM news_archive
WHERE text_status = 'slug' AND origin = 'wayback'
ORDER BY (tokens && %s::text[]) DESC, COALESCE(published_on, captured_at::date) DESC NULLS LAST
LIMIT %s
"""
_ENRICH = """
UPDATE news_archive SET
  title = CASE WHEN %s <> '' THEN %s ELSE title END,
  summary = CASE WHEN length(%s) > length(summary) THEN %s ELSE summary END,
  text_status = %s,
  published_on = CASE WHEN %s::date IS NOT NULL THEN %s::date ELSE published_on END,
  published_source = CASE WHEN %s::date IS NOT NULL THEN 'page' ELSE published_source END,
  tokens = ARRAY(SELECT DISTINCT unnest(tokens || %s::text[]))
WHERE url = %s
"""
_SAVE_FEED = """
INSERT INTO feed_registry (feed_url, source_id, site_url, title, status, entries, detail, checked_at, last_success)
VALUES (%s, %s, %s, %s, %s, %s, %s, now(), CASE WHEN %s = 'active' THEN now() ELSE NULL END)
ON CONFLICT (feed_url) DO UPDATE SET
  source_id = EXCLUDED.source_id, site_url = EXCLUDED.site_url, title = EXCLUDED.title, status = EXCLUDED.status,
  entries = EXCLUDED.entries, detail = EXCLUDED.detail, checked_at = now(),
  last_success = COALESCE(EXCLUDED.last_success, feed_registry.last_success)
"""
_ACTIVE_FEEDS = "SELECT feed_url FROM feed_registry WHERE status = 'active' ORDER BY feed_url"
_CORPUS_SEARCH = f"""
SELECT paper_id, title, abstract, created_on, categories
FROM corpus_papers
WHERE tokens && %s::text[] AND {_MATCHED} >= %s AND created_on >= %s
ORDER BY {_MATCHED} DESC, created_on DESC
LIMIT %s
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
_PAPER_TITLES = f"""
SELECT title, created_on
FROM corpus_papers
WHERE tokens && %s::text[] AND {_MATCHED} >= %s AND created_on >= %s AND created_on < %s
  AND (%s::text[] IS NULL OR tokens && %s::text[])
ORDER BY md5(paper_id)
LIMIT %s
"""
_HARVEST_STATE = """
SELECT set_spec, resumption_token, records, kept, status, watermark FROM corpus_harvest WHERE set_spec = %s
"""
_SAVE_HARVEST = """
INSERT INTO corpus_harvest (set_spec, resumption_token, records, kept, status, detail, watermark, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (set_spec) DO UPDATE SET
  resumption_token = EXCLUDED.resumption_token, records = EXCLUDED.records, kept = EXCLUDED.kept,
  status = EXCLUDED.status, detail = EXCLUDED.detail, watermark = EXCLUDED.watermark, updated_at = now()
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
                 list(e.tokens), e.published_source, e.text_status) for e in entries if e.title and e.tokens]
        for start in range(0, len(rows), _BATCH):
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.executemany(_SAVE_ENTRY, rows[start : start + _BATCH])
        return len(rows)

    async def search(self, words: Sequence[str], since: date, limit: int, min_match: int) -> list[ArchiveEntry]:
        keys = list(words)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SEARCH, (keys, keys, min_match, since, since, keys, limit))
            rows = await cur.fetchall()
        return [_entry(r) for r in rows]

    async def titles_matching(self, keywords: Sequence[str], min_overlap: int, start: date, end: date,
                              limit: int, origin: str | None = None,
                              anchors: Sequence[str] | None = None) -> list[tuple[str, date | None]]:
        # Пул проекта отдаёт строки словарями (ws_common.db: row_factory = dict_row) — только обращение по имени
        # колонки (этап 4.3.1).
        keys, anchor = list(keywords), (list(anchors) if anchors else None)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_TITLES, (keys, keys, min_overlap, start, end, origin, origin, anchor, anchor, limit))
            return [(row["title"], row["published_on"]) for row in await cur.fetchall()]

    async def event_candidates(self, vocabulary: Sequence[str], cues: Sequence[str], min_overlap: int, since: date,
                               limit: int) -> list[tuple[ArchiveEntry, tuple[str, ...]]]:
        words = list(vocabulary)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_EVENTS, (words, words, list(cues), words, min_overlap, since, limit))
            rows = await cur.fetchall()
        return [(_entry(r), tuple(r["matched"] or ())) for r in rows]

    async def entries_from_collections(self, limit: int) -> list[ArchiveEntry]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FROM_COLLECTIONS, (limit,))
            return [_entry(r) for r in await cur.fetchall()]

    async def harvest_states(self, publisher: str) -> dict[str, tuple[str, str | None]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PERIODS, (publisher,))
            return {row["period"]: (row["status"], row["resume"]) for row in await cur.fetchall()}

    async def mark_harvest(self, publisher: str, period: str, status: str, rows: int, detail: str,
                           resume: str | None = None) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_MARK, (publisher, period, status, rows, detail[:500], resume))

    async def entries_to_enrich(self, cues: Sequence[str], limit: int) -> list[ArchiveEntry]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_TO_ENRICH, (list(cues), limit))
            return [_entry(r) for r in await cur.fetchall()]

    async def save_enrichment(self, url: str, title: str, summary: str, published_on: date | None,
                              text_status: str, tokens: Sequence[str]) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ENRICH, (title[:512], title[:512], summary[:2000], summary[:2000], text_status,
                                        published_on, published_on, published_on, list(tokens), url))

    async def save_feed(self, feed_url: str, source_id: str, site_url: str, title: str, status: str, entries: int,
                        detail: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SAVE_FEED, (feed_url, source_id, site_url, title[:300], status, entries, detail[:500], status))

    async def active_feeds(self) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_ACTIVE_FEEDS)
            return [row["feed_url"] for row in await cur.fetchall()]

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

    async def titles_matching(self, keywords: Sequence[str], min_overlap: int, start: date, end: date,
                              limit: int, anchors: Sequence[str] | None = None) -> list[tuple[str, date | None]]:
        keys, anchor = list(keywords), (list(anchors) if anchors else None)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PAPER_TITLES, (keys, keys, min_overlap, start, end, anchor, anchor, limit))
            return [(row["title"], row["created_on"]) for row in await cur.fetchall()]

    async def harvest_state(self, set_spec: str) -> CorpusHarvestState | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_HARVEST_STATE, (set_spec,))
            row = await cur.fetchone()
        if row is None:
            return None
        return CorpusHarvestState(row["set_spec"], row["resumption_token"], row["records"], row["kept"], row["status"],
                                  row["watermark"])

    async def save_harvest_state(self, state: CorpusHarvestState, detail: str = "") -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SAVE_HARVEST, (state.set_spec, state.resumption_token, state.records, state.kept,
                                              state.status, detail[:500], state.watermark))

    async def search(self, words: Sequence[str], since: date, limit: int, min_match: int) -> list[CorpusPaper]:
        keys = list(words)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_CORPUS_SEARCH, (keys, keys, min_match, since, keys, limit))
            rows = await cur.fetchall()
        return [CorpusPaper(paper_id=r["paper_id"], title=r["title"], created_on=r["created_on"],
                            abstract=r["abstract"], categories=tuple(r["categories"] or ())) for r in rows]

    async def stats(self) -> list[dict[str, object]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_CORPUS_STATS)
            return list(await cur.fetchall())


def _entry(row: dict[str, Any]) -> ArchiveEntry:
    return ArchiveEntry(url=row["url"], publisher=row["publisher"], origin=row["origin"], title=row["title"],
                        published_on=row["published_on"], captured_at=row["captured_at"], summary=row["summary"],
                        published_source=row.get("published_source", "unknown"), text_status=row.get("text_status", "slug"))
