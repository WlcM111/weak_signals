"""Репозиторий записей входов анализа (аудит A3, режим А; этап 2)."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from analyzer.application.capture import AnalysisCapture, pack_vectors, vectors_digest
from ws_common.jsonsafe import json_safe

_UPSERT_CAPTURE = """
INSERT INTO analysis_captures (
  analysis_id, as_of, model_version_id, embedding_model, query_text, params, config, funnel, encyclopedia,
  vectors, vectors_sha256
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (analysis_id) DO UPDATE SET
  as_of = EXCLUDED.as_of, model_version_id = EXCLUDED.model_version_id, embedding_model = EXCLUDED.embedding_model,
  query_text = EXCLUDED.query_text, params = EXCLUDED.params, config = EXCLUDED.config, funnel = EXCLUDED.funnel,
  encyclopedia = EXCLUDED.encyclopedia, vectors = EXCLUDED.vectors, vectors_sha256 = EXCLUDED.vectors_sha256,
  created_at = now()
"""


class PostgresCaptureRepository:
    """Реализация порта `CaptureRepository` (синхронный psycopg, как весь analyzer)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def save(self, analysis_id: str, capture: AnalysisCapture) -> None:
        """Пишет запись одной командой; векторы — архивом npz с контрольной суммой."""
        blob = pack_vectors(capture)
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _UPSERT_CAPTURE,
                (
                    analysis_id,
                    capture.as_of,
                    capture.model_version_id,
                    capture.embedding_model,
                    capture.query_text,
                    Json(json_safe(capture.params)),
                    Json(json_safe(capture.config)),
                    Json(json_safe(capture.funnel)),
                    Json(json_safe(capture.encyclopedia)),
                    blob,
                    vectors_digest(blob),
                ),
            )
