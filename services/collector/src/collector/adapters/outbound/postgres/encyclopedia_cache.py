"""Кеш проверок Wikipedia (таблица `encyclopedia_cache`, TTL из конфигурации)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from collector.application.dto import EncyclopediaHit

_SELECT = """
SELECT title_norm, exists_flag, page_url, pageviews_30d, page_created_at
FROM encyclopedia_cache
WHERE language_code = %s AND title_norm = ANY(%s::text[]) AND checked_at >= %s
"""

_UPSERT = """
INSERT INTO encyclopedia_cache (
  language_code, title_norm, exists_flag, page_url, pageviews_30d, page_created_at, checked_at
) VALUES (%s, %s, %s, %s, %s, %s, now())
ON CONFLICT (language_code, title_norm) DO UPDATE SET
  exists_flag = EXCLUDED.exists_flag,
  page_url = EXCLUDED.page_url,
  pageviews_30d = EXCLUDED.pageviews_30d,
  page_created_at = EXCLUDED.page_created_at,
  checked_at = now()
"""

_PURGE = "DELETE FROM encyclopedia_cache WHERE checked_at < %s RETURNING title_norm"


class PostgresEncyclopediaCache:
    """Реализация порта `EncyclopediaCacheRepository`."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def get_many(
        self, language_code: str, title_norms: Sequence[str], fresh_after: datetime
    ) -> dict[str, EncyclopediaHit]:
        """Свежие записи кеша по нормализованным названиям."""
        if not title_norms:
            return {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT, (language_code, list(title_norms), fresh_after))
            rows = await cur.fetchall()
        return {
            row["title_norm"]: EncyclopediaHit(
                title=row["title_norm"],
                exists=bool(row["exists_flag"]),
                page_url=row["page_url"] or "",
                pageviews_30d=int(row["pageviews_30d"]),
                created_at=row["page_created_at"],
            )
            for row in rows
        }

    async def put(self, language_code: str, title_norm: str, hit: EncyclopediaHit) -> None:
        """Сохраняет результат проверки статьи."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _UPSERT,
                (
                    language_code,
                    title_norm,
                    hit.exists,
                    hit.page_url or None,
                    hit.pageviews_30d,
                    hit.created_at,
                ),
            )

    async def purge_older_than(self, moment: datetime) -> int:
        """Удаляет устаревшие записи кеша."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_PURGE, (moment,))
            rows = await cur.fetchall()
        return len(rows)
