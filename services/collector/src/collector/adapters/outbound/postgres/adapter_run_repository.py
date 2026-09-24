"""Репозиторий запусков адаптеров (таблица `adapter_runs`)."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from collector.adapters.outbound.postgres.mappers import ADAPTER_RUN_COLUMNS, row_to_adapter_run
from collector.domain.entities import AdapterRun

_UPSERT_RUN = """
INSERT INTO adapter_runs (
  collection_id, source_key, status, http_requests, documents_found, documents_new,
  error_code, error_message, started_at, finished_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)
ON CONFLICT (collection_id, source_key) DO UPDATE SET
  status = EXCLUDED.status,
  http_requests = EXCLUDED.http_requests,
  documents_found = EXCLUDED.documents_found,
  documents_new = EXCLUDED.documents_new,
  error_code = EXCLUDED.error_code,
  error_message = EXCLUDED.error_message,
  started_at = COALESCE(adapter_runs.started_at, EXCLUDED.started_at),
  finished_at = EXCLUDED.finished_at
"""

_LIST_RUNS = (
    f"SELECT {ADAPTER_RUN_COLUMNS} FROM adapter_runs WHERE collection_id = %s ORDER BY source_key"
)


class PostgresAdapterRunRepository:
    """Реализация порта `AdapterRunRepository`."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def save(self, collection_id: str, run: AdapterRun) -> None:
        """Сохраняет статус и счётчики запуска (идемпотентно по (collection_id, source_key))."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _UPSERT_RUN,
                (
                    collection_id,
                    run.source_key.value,
                    run.status.value,
                    run.http_requests,
                    run.documents_found,
                    run.documents_new,
                    run.error_code or None,
                    run.error_message or None,
                    run.started_at,
                    run.finished_at,
                ),
            )

    async def list_runs(self, collection_id: str) -> list[AdapterRun]:
        """Все запуски адаптеров коллекции."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LIST_RUNS, (collection_id,))
            rows = await cur.fetchall()
        return [row_to_adapter_run(dict(row)) for row in rows]
