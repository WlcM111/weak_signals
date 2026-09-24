"""Чтение каталога источников (`source_catalog`) при старте сервиса."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from collector.domain.values import SourceKey

_SELECT_CATALOG = (
    "SELECT source_key, requires_api_key, rate_limit_rps, enabled FROM source_catalog ORDER BY source_key"
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Строка каталога источников."""

    source_key: SourceKey
    requires_api_key: bool
    rate_limit_rps: float
    enabled: bool


class PostgresSourceCatalog:
    """Справочник адаптеров: включённость и лимиты частоты запросов."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def load(self) -> dict[SourceKey, CatalogEntry]:
        """Каталог в виде словаря по ключу источника."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_CATALOG)
            rows = await cur.fetchall()
        return {
            SourceKey(row["source_key"]): CatalogEntry(
                source_key=SourceKey(row["source_key"]),
                requires_api_key=bool(row["requires_api_key"]),
                rate_limit_rps=float(row["rate_limit_rps"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        }
