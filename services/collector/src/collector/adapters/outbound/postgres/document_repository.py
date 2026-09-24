"""Репозиторий документов: upsert по `url_hash`, связь с коллекцией, счётчики, ретенция."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from collector.adapters.outbound.postgres.mappers import DOCUMENT_COLUMNS, qualify, row_to_document
from collector.application.dto import AdapterDelta, BatchItem, BatchOutcome
from collector.domain.entities import Document, DocumentDraft
from collector.domain.values import SourceKey

_FIND_BY_DOI = "SELECT document_id FROM documents WHERE doi = %s LIMIT 1"

_UPSERT_DOCUMENT = """
INSERT INTO documents (
  url_hash, url, canonical_url, origin_domain, title, body_text, content_hash, language_code,
  published_at, source_key, source_type, trust_level, doi, citation_count, engagement_count,
  raw_meta, fetched_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (url_hash) DO UPDATE SET fetched_at = EXCLUDED.fetched_at
RETURNING document_id, (xmax = 0) AS inserted
"""

_LINK_DOCUMENT = """
INSERT INTO collection_documents (collection_id, document_id, source_key, relevance_rank, matched_term)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (collection_id, document_id) DO NOTHING
RETURNING document_id
"""

_BUMP_COLLECTION = """
UPDATE collections
SET documents_total = documents_total + %s, http_requests_total = http_requests_total + %s
WHERE collection_id = %s
RETURNING documents_total
"""

_BUMP_ADAPTER_RUN = """
UPDATE adapter_runs
SET http_requests = http_requests + %s, documents_found = documents_found + %s, documents_new = documents_new + %s
WHERE collection_id = %s AND source_key = %s
"""

_SELECT_MANY = f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE document_id = ANY(%s::uuid[])"

_PAGE = f"""
SELECT cd.relevance_rank, {qualify(DOCUMENT_COLUMNS, "d")}
FROM collection_documents AS cd
JOIN documents AS d USING (document_id)
WHERE cd.collection_id = %s AND (cd.relevance_rank, cd.document_id) > (%s, %s::uuid)
ORDER BY cd.relevance_rank, cd.document_id
LIMIT %s
"""

_PAGE_FIRST = f"""
SELECT cd.relevance_rank, {qualify(DOCUMENT_COLUMNS, "d")}
FROM collection_documents AS cd
JOIN documents AS d USING (document_id)
WHERE cd.collection_id = %s
ORDER BY cd.relevance_rank, cd.document_id
LIMIT %s
"""

_PURGE_ORPHANS = """
WITH victims AS (
  SELECT d.document_id
  FROM documents AS d
  WHERE d.fetched_at < %(threshold)s
    AND NOT EXISTS (
      SELECT 1
      FROM collection_documents AS cd
      JOIN collections AS c USING (collection_id)
      WHERE cd.document_id = d.document_id AND c.created_at >= %(threshold)s
    )
  LIMIT %(batch_size)s
)
DELETE FROM documents WHERE document_id IN (SELECT document_id FROM victims)
RETURNING document_id
"""


class PostgresDocumentRepository:
    """Реализация порта `DocumentRepository`; запись партии — одна транзакция (§11.8 ТЗ)."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def persist_batch(
        self,
        collection_id: str,
        items: Sequence[BatchItem],
        deltas: Mapping[SourceKey, AdapterDelta],
    ) -> BatchOutcome:
        """Записывает документы, связи и приросты счётчиков атомарно."""
        new_by_source: dict[SourceKey, int] = {}
        linked = 0
        new_documents = 0
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # единый порядок блокировок строк documents: две параллельные коллекции с общими URL
            # иначе могли взаимно заблокироваться (deadlock) на ON CONFLICT DO UPDATE
            for item in sorted(items, key=lambda entry: entry.draft.url_hash):
                document_id, inserted = await self._upsert(cur, item.draft)
                await cur.execute(
                    _LINK_DOCUMENT,
                    (
                        collection_id,
                        document_id,
                        item.draft.source_key.value,
                        item.relevance_rank,
                        item.draft.matched_term,
                    ),
                )
                if await cur.fetchone() is None:
                    continue  # документ уже был в этой коллекции
                linked += 1
                if inserted:
                    new_documents += 1
                    new_by_source[item.draft.source_key] = new_by_source.get(item.draft.source_key, 0) + 1
            http_total = sum(delta.http_requests for delta in deltas.values())
            await cur.execute(_BUMP_COLLECTION, (linked, http_total, collection_id))
            row = await cur.fetchone()
            documents_total = int(row["documents_total"]) if row else 0
            for source_key in set(deltas) | set(new_by_source):
                delta = deltas.get(source_key, AdapterDelta())
                await cur.execute(
                    _BUMP_ADAPTER_RUN,
                    (
                        delta.http_requests,
                        delta.documents_found,
                        new_by_source.get(source_key, 0),
                        collection_id,
                        source_key.value,
                    ),
                )
        return BatchOutcome(
            documents_total=documents_total,
            new_documents=new_documents,
            linked_documents=linked,
            new_by_source=new_by_source,
        )

    async def _upsert(self, cur: Any, draft: DocumentDraft) -> tuple[str, bool]:
        """Вставляет документ или находит существующий по DOI/url_hash; True = документ новый."""
        if draft.doi:
            await cur.execute(_FIND_BY_DOI, (draft.doi,))
            existing = await cur.fetchone()
            if existing is not None:
                return str(existing["document_id"]), False
        await cur.execute(
            _UPSERT_DOCUMENT,
            (
                draft.url_hash,
                draft.url,
                draft.canonical_url,
                draft.origin_domain,
                draft.title,
                draft.text,
                draft.content_hash,
                draft.language_code,
                draft.published_at,
                draft.source_key.value,
                draft.source_type.value,
                draft.trust_level.value,
                draft.doi,
                draft.citation_count,
                draft.engagement_count,
                Json(draft.raw_meta),
                draft.fetched_at,
            ),
        )
        row = await cur.fetchone()
        return str(row["document_id"]), bool(row["inserted"])

    async def get_many(self, document_ids: Sequence[str]) -> list[Document]:
        """Документы по идентификаторам."""
        if not document_ids:
            return []
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_MANY, (list(document_ids),))
            rows = await cur.fetchall()
        return [row_to_document(dict(row)) for row in rows]

    async def page(
        self, collection_id: str, after: tuple[int, str] | None, limit: int
    ) -> list[tuple[int, Document]]:
        """Keyset-страница документов коллекции."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            if after is None:
                await cur.execute(_PAGE_FIRST, (collection_id, limit))
            else:
                await cur.execute(_PAGE, (collection_id, after[0], after[1], limit))
            rows = await cur.fetchall()
        return [(int(row["relevance_rank"]), row_to_document(dict(row))) for row in rows]

    async def purge_orphans(self, older_than: datetime, batch_size: int) -> int:
        """Удаляет документы вне актуальных коллекций (ретенция §11.9)."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_PURGE_ORPHANS, {"threshold": older_than, "batch_size": batch_size})
            rows = await cur.fetchall()
        return len(rows)
