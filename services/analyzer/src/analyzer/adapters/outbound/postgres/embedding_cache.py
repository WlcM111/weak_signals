"""Кеш эмбеддингов документов (таблица `document_embeddings`, без pgvector)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from analyzer.application.dto import CachedEmbedding

_SELECT = (
    "SELECT document_id, content_hash, vector FROM document_embeddings "
    "WHERE embedding_model = %s AND document_id = ANY(%s::uuid[])"
)

_UPSERT = """
INSERT INTO document_embeddings (document_id, embedding_model, content_hash, dims, vector)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (document_id, embedding_model) DO UPDATE SET
  content_hash = EXCLUDED.content_hash,
  dims = EXCLUDED.dims,
  vector = EXCLUDED.vector,
  created_at = now()
"""


class PostgresEmbeddingCache:
    """Реализация порта `EmbeddingCache`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def get_many(self, document_ids: Sequence[str], embedding_model: str) -> dict[str, CachedEmbedding]:
        """Записи кеша по идентификаторам документов."""
        if not document_ids:
            return {}
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_SELECT, (embedding_model, list(document_ids)))
            rows = cur.fetchall()
        return {
            str(row["document_id"]): CachedEmbedding(
                document_id=str(row["document_id"]),
                content_hash=row["content_hash"],
                vector=np.asarray(row["vector"], dtype=np.float32),
            )
            for row in rows
        }

    def put_many(self, entries: Sequence[CachedEmbedding], embedding_model: str, dims: int) -> None:
        """Сохраняет вычисленные эмбеддинги пачкой."""
        if not entries:
            return
        payload = [
            (
                entry.document_id,
                embedding_model,
                entry.content_hash,
                dims,
                [float(value) for value in np.asarray(entry.vector).ravel()],
            )
            for entry in entries
        ]
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(_UPSERT, payload)
