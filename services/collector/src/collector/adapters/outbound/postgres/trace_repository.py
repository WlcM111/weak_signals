"""Репозиторий трассы сбора: сырые ответы источников и события документов (аудит A3, этап 2)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from collector.application.trace_recording import EventRecord, ResponseRecord
from ws_common.jsonsafe import json_safe

_INSERT_RESPONSE = """
INSERT INTO raw_responses (
  collection_id, run_id, seq, source_key, method, url, request_body, outcome, error_message,
  body_gzip, body_sha256, body_bytes, started_at, duration_ms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_EVENT = """
INSERT INTO document_events (
  collection_id, run_id, seq, source_key, matched_term, event, url, url_hash, doi, title, detail
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PostgresCollectionTraceRepository:
    """Реализация порта `CollectionTraceRepository`: партия записей — одна транзакция."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def append(
        self,
        collection_id: str,
        run_id: str,
        responses: Sequence[ResponseRecord],
        events: Sequence[EventRecord],
    ) -> None:
        """Дописывает ответы и события; повторная попытка сбора пишется со своим `run_id`."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            if responses:
                await cur.executemany(
                    _INSERT_RESPONSE,
                    [
                        (
                            collection_id, run_id, item.seq, item.source_key, item.method, item.url,
                            Json(json_safe(item.request_body)) if item.request_body is not None else None, item.outcome,
                            item.error_message, item.body_gzip, item.body_sha256, item.body_bytes, item.started_at,
                            item.duration_ms,
                        )
                        for item in responses
                    ],
                )
            if events:
                await cur.executemany(
                    _INSERT_EVENT,
                    [
                        (
                            collection_id, run_id, item.seq, item.source_key, item.matched_term, item.event.value,
                            item.url, item.url_hash, item.doi, item.title, item.detail,
                        )
                        for item in events
                    ],
                )
