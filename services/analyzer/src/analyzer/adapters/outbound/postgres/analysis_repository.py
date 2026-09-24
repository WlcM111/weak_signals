"""Репозиторий анализов: очередь, аренды, идемпотентность (таблица `analyses`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from analyzer.adapters.outbound.postgres.mappers import ANALYSIS_COLUMNS, qualify, row_to_analysis
from analyzer.application.dto import AnalysisDraft, LeaseState
from analyzer.domain.entities import Analysis
from analyzer.domain.values import AnalysisStats, OperationStatus

_INSERT = f"""
INSERT INTO analyses (
  idempotency_key, collection_id, query_text, model_version_id,
  top_n, max_candidates, weak_signal_threshold, min_evidence_documents
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING {ANALYSIS_COLUMNS}
"""

_SELECT_BY_KEY = f"SELECT {ANALYSIS_COLUMNS} FROM analyses WHERE idempotency_key = %s"
_SELECT_BY_ID = f"SELECT {ANALYSIS_COLUMNS} FROM analyses WHERE analysis_id = %s"

_CLAIM_NEXT = f"""
WITH candidate AS (
  SELECT analysis_id FROM analyses
  WHERE status = 'PENDING' AND cancel_requested = false
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE analyses AS a
SET status = 'RUNNING',
    lease_owner = %s,
    lease_expires_at = now() + make_interval(secs => %s),
    started_at = COALESCE(a.started_at, now())
FROM candidate
WHERE a.analysis_id = candidate.analysis_id
RETURNING {qualify(ANALYSIS_COLUMNS, "a")}
"""

_HEARTBEAT = """
UPDATE analyses
SET lease_expires_at = now() + make_interval(secs => %s)
WHERE analysis_id = %s AND lease_owner = %s AND status = 'RUNNING'
RETURNING cancel_requested
"""

_REQUEST_CANCEL = """
UPDATE analyses
SET cancel_requested = true,
    status = CASE WHEN status = 'PENDING' THEN 'CANCELLED' ELSE status END,
    error_code = CASE WHEN status = 'PENDING' THEN 'CANCELLED' ELSE error_code END,
    error_message = CASE WHEN status = 'PENDING' THEN 'анализ отменён до запуска' ELSE error_message END,
    finished_at = CASE WHEN status = 'PENDING' THEN now() ELSE finished_at END,
    lease_owner = CASE WHEN status = 'PENDING' THEN NULL ELSE lease_owner END,
    lease_expires_at = CASE WHEN status = 'PENDING' THEN NULL ELSE lease_expires_at END
WHERE analysis_id = %s
RETURNING status
"""

_FINISH = """
UPDATE analyses
SET status = %s, error_code = %s, error_message = %s, finished_at = %s,
    documents_input = %s, documents_after_dedup = %s, clusters_total = %s, candidates_scored = %s,
    duration_ms = %s, lease_owner = NULL, lease_expires_at = NULL
WHERE analysis_id = %s AND lease_owner = %s AND status = 'RUNNING'
RETURNING analysis_id
"""

_RELEASE_EXPIRED = """
UPDATE analyses
SET status = 'PENDING', lease_owner = NULL, lease_expires_at = NULL
WHERE status = 'RUNNING' AND lease_expires_at < now()
RETURNING analysis_id
"""


class PostgresAnalysisRepository:
    """Реализация порта `AnalysisRepository` на синхронном psycopg 3."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def create_if_absent(self, draft: AnalysisDraft) -> tuple[Analysis, bool]:
        """Создаёт анализ; при конфликте по ключу возвращает существующий."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _INSERT,
                (
                    draft.idempotency_key,
                    draft.collection_id,
                    draft.query_text,
                    draft.model_version_id,
                    draft.params.top_n,
                    draft.params.max_candidates,
                    draft.params.weak_signal_threshold,
                    draft.params.min_evidence_documents,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                return row_to_analysis(dict(row)), False
            cur.execute(_SELECT_BY_KEY, (draft.idempotency_key,))
            existing = cur.fetchone()
        if existing is None:
            raise RuntimeError("анализ с таким idempotency_key недоступен для чтения")
        return row_to_analysis(dict(existing)), True

    def get(self, analysis_id: str) -> Analysis | None:
        """Анализ по идентификатору."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_BY_ID, (analysis_id,))
            row = cur.fetchone()
        return row_to_analysis(dict(row)) if row else None

    def count_pending(self) -> int:
        """Число анализов в очереди и в работе."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS total FROM analyses WHERE status IN ('PENDING','RUNNING')")
            row = cur.fetchone()
        return int(row["total"]) if row else 0

    def claim_next(self, owner: str, lease_seconds: int) -> Analysis | None:
        """Захват следующего анализа очереди с арендой."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_CLAIM_NEXT, (owner, lease_seconds))
            row = cur.fetchone()
        return row_to_analysis(dict(row)) if row else None

    def heartbeat(self, analysis_id: str, owner: str, lease_seconds: int) -> LeaseState:
        """Продление аренды; отсутствие строки означает потерю аренды."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_HEARTBEAT, (lease_seconds, analysis_id, owner))
            row = cur.fetchone()
        if row is None:
            return LeaseState(alive=False, cancel_requested=False)
        return LeaseState(alive=True, cancel_requested=bool(row["cancel_requested"]))

    def request_cancel(self, analysis_id: str) -> OperationStatus | None:
        """Запрос отмены; для PENDING сразу переводит в CANCELLED."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_REQUEST_CANCEL, (analysis_id,))
            row = cur.fetchone()
        return OperationStatus(row["status"]) if row else None

    def finish(
        self,
        analysis_id: str,
        owner: str,
        status: OperationStatus,
        *,
        stats: AnalysisStats,
        error_code: str,
        error_message: str,
        finished_at: datetime,
    ) -> bool:
        """Терминальное завершение анализа владельцем аренды (без записи кандидатов)."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _FINISH,
                (
                    status.value,
                    error_code or None,
                    error_message or None,
                    finished_at,
                    stats.documents_input,
                    stats.documents_after_dedup,
                    stats.clusters_total,
                    stats.candidates_scored,
                    stats.duration_ms,
                    analysis_id,
                    owner,
                ),
            )
            row = cur.fetchone()
        return row is not None

    def release_expired_leases(self) -> int:
        """Возврат анализов с истёкшей арендой в очередь."""
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_RELEASE_EXPIRED)
            rows = cur.fetchall()
        return len(rows)
