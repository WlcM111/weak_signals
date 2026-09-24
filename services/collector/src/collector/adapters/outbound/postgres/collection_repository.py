"""Репозиторий коллекций: очередь, аренды, идемпотентность (таблица `collections`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from collector.adapters.outbound.postgres.mappers import COLLECTION_COLUMNS, qualify, row_to_collection
from collector.application.dto import CollectionDraft, LeaseState
from collector.domain.entities import Collection
from collector.domain.values import OperationStatus, SourceKey

_INSERT_COLLECTION = f"""
INSERT INTO collections (
  idempotency_key, query_text, mode, terms_ru, terms_en,
  max_documents_per_source, max_total_documents, time_budget_seconds, published_since_year
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING {COLLECTION_COLUMNS}
"""

_INSERT_ADAPTER_RUNS = """
INSERT INTO adapter_runs (collection_id, source_key, status)
SELECT %s, key, 'PENDING' FROM unnest(%s::text[]) AS key
ON CONFLICT (collection_id, source_key) DO NOTHING
"""

_SELECT_BY_KEY = f"SELECT {COLLECTION_COLUMNS} FROM collections WHERE idempotency_key = %s"
_SELECT_BY_ID = f"SELECT {COLLECTION_COLUMNS} FROM collections WHERE collection_id = %s"

_CLAIM_NEXT = f"""
WITH candidate AS (
  SELECT collection_id FROM collections
  WHERE status = 'PENDING' AND cancel_requested = false
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE collections AS c
SET status = 'RUNNING',
    lease_owner = %s,
    lease_expires_at = now() + make_interval(secs => %s),
    started_at = COALESCE(c.started_at, now())
FROM candidate
WHERE c.collection_id = candidate.collection_id
RETURNING {qualify(COLLECTION_COLUMNS, "c")}
"""

_HEARTBEAT = """
UPDATE collections
SET lease_expires_at = now() + make_interval(secs => %s)
WHERE collection_id = %s AND lease_owner = %s AND status = 'RUNNING'
RETURNING cancel_requested
"""

_REQUEST_CANCEL = """
UPDATE collections
SET cancel_requested = true,
    status = CASE WHEN status = 'PENDING' THEN 'CANCELLED' ELSE status END,
    error_code = CASE WHEN status = 'PENDING' THEN 'CANCELLED' ELSE error_code END,
    error_message = CASE WHEN status = 'PENDING' THEN 'сбор отменён до запуска' ELSE error_message END,
    finished_at = CASE WHEN status = 'PENDING' THEN now() ELSE finished_at END,
    lease_owner = CASE WHEN status = 'PENDING' THEN NULL ELSE lease_owner END,
    lease_expires_at = CASE WHEN status = 'PENDING' THEN NULL ELSE lease_expires_at END
WHERE collection_id = %s
RETURNING status
"""

_FINISH = """
UPDATE collections
SET status = %s, error_code = %s, error_message = %s, finished_at = %s,
    lease_owner = NULL, lease_expires_at = NULL
WHERE collection_id = %s AND lease_owner = %s AND status = 'RUNNING'
RETURNING collection_id
"""

_RELEASE_EXPIRED = """
UPDATE collections
SET status = 'PENDING', lease_owner = NULL, lease_expires_at = NULL
WHERE status = 'RUNNING' AND lease_expires_at < now()
RETURNING collection_id
"""


class PostgresCollectionRepository:
    """Реализация порта `CollectionRepository` на psycopg 3."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def create_if_absent(self, draft: CollectionDraft) -> tuple[Collection, bool]:
        """Создаёт коллекцию и строки `adapter_runs` в одной транзакции (идемпотентно по ключу)."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _INSERT_COLLECTION,
                (
                    draft.idempotency_key,
                    draft.query_text,
                    draft.mode.value,
                    list(draft.terms.ru),
                    list(draft.terms.en),
                    draft.limits.max_documents_per_source,
                    draft.limits.max_total_documents,
                    draft.limits.time_budget_seconds,
                    draft.limits.published_since_year,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(_SELECT_BY_KEY, (draft.idempotency_key,))
                existing = await cur.fetchone()
                if existing is None:  # строка исчезла между конфликтом и чтением — повтор вызова
                    raise RuntimeError("коллекция с таким idempotency_key недоступна для чтения")
                return row_to_collection(dict(existing)), True
            collection = row_to_collection(dict(row))
            await cur.execute(
                _INSERT_ADAPTER_RUNS,
                (collection.collection_id, [source.value for source in draft.sources]),
            )
        return collection, False

    async def get(self, collection_id: str) -> Collection | None:
        """Коллекция по идентификатору."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_BY_ID, (collection_id,))
            row = await cur.fetchone()
        return row_to_collection(dict(row)) if row else None

    async def count_pending(self) -> int:
        """Число коллекций в очереди и в работе."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) AS total FROM collections WHERE status IN ('PENDING','RUNNING')")
            row = await cur.fetchone()
        return int(row["total"]) if row else 0

    async def claim_next(self, owner: str, lease_seconds: int) -> Collection | None:
        """Захват следующей коллекции очереди с арендой (`FOR UPDATE SKIP LOCKED`)."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_CLAIM_NEXT, (owner, lease_seconds))
            row = await cur.fetchone()
        return row_to_collection(dict(row)) if row else None

    async def heartbeat(self, collection_id: str, owner: str, lease_seconds: int) -> LeaseState:
        """Продление аренды; отсутствие строки означает потерю аренды."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HEARTBEAT, (lease_seconds, collection_id, owner))
            row = await cur.fetchone()
        if row is None:
            return LeaseState(alive=False, cancel_requested=False)
        return LeaseState(alive=True, cancel_requested=bool(row["cancel_requested"]))

    async def request_cancel(self, collection_id: str) -> OperationStatus | None:
        """Запрос отмены; для PENDING сразу переводит в CANCELLED."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_REQUEST_CANCEL, (collection_id,))
            row = await cur.fetchone()
        return OperationStatus(row["status"]) if row else None

    async def finish(
        self,
        collection_id: str,
        owner: str,
        status: OperationStatus,
        *,
        error_code: str,
        error_message: str,
        finished_at: datetime,
    ) -> bool:
        """Терминальное завершение коллекции владельцем аренды."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _FINISH,
                (status.value, error_code or None, error_message or None, finished_at, collection_id, owner),
            )
            row = await cur.fetchone()
        return row is not None

    async def release_expired_leases(self) -> int:
        """Возврат коллекций с истёкшей арендой в очередь."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_RELEASE_EXPIRED)
            rows = await cur.fetchall()
        return len(rows)

    async def list_sources(self, collection_id: str) -> tuple[SourceKey, ...]:
        """Источники коллекции по строкам `adapter_runs` (для диагностики и восстановления)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT source_key FROM adapter_runs WHERE collection_id = %s ORDER BY source_key",
                (collection_id,),
            )
            rows = await cur.fetchall()
        return tuple(SourceKey(row["source_key"]) for row in rows)
