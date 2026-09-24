"""Репозиторий ключей идемпотентности HTTP (таблица `idempotency_keys`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

_SELECT = (
    "SELECT job_id, request_hash FROM idempotency_keys "
    "WHERE idempotency_key = %s AND expires_at > now()"
)
_PURGE = "DELETE FROM idempotency_keys WHERE expires_at <= %s RETURNING idempotency_key"


class PostgresIdempotencyRepository:
    """Реализация порта `IdempotencyRepository`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self, key: str) -> tuple[str, str] | None:
        """Возвращает (`job_id`, `request_hash`), если ключ существует и не истёк."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT, (key,))
            row = await cur.fetchone()
        return (str(row["job_id"]), row["request_hash"]) if row else None

    async def purge_expired(self, now: datetime) -> int:
        """Удаляет истёкшие ключи."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_PURGE, (now,))
            rows = await cur.fetchall()
        return len(rows)
