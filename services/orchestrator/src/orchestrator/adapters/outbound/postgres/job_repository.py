"""Репозиторий заданий: очередь с арендами, переходы статусов и журнал событий."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orchestrator.adapters.outbound.postgres.mappers import JOB_COLUMNS, row_to_job
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.values import MAX_POSTPONEMENTS, JobStatus

_INSERT_QUERY = """
INSERT INTO queries (query_id, query_text, normalized_text, requested_top_n, client_ip_hash)
VALUES (%s, %s, %s, %s, %s)
"""
_INSERT_JOB = "INSERT INTO jobs (job_id, query_id, status) VALUES (%s, %s, 'QUEUED')"
_INSERT_KEY = """
INSERT INTO idempotency_keys (idempotency_key, request_hash, job_id, expires_at)
VALUES (%s, %s, %s, %s)
"""
_INSERT_EVENT = """
INSERT INTO job_events (job_id, from_status, to_status, worker_id, detail)
VALUES (%s, %s, %s, %s, %s)
"""
_SELECT_JOB = f"""
SELECT {JOB_COLUMNS} FROM jobs j JOIN queries q ON q.query_id = j.query_id WHERE j.job_id = %s
"""
_COUNT_PENDING = (
    "SELECT count(*) AS total FROM jobs "
    "WHERE status IN ('QUEUED','COLLECTING','ANALYZING','NARRATING')"
)
_CLAIM = f"""
WITH candidate AS (
  SELECT job_id FROM jobs
  WHERE cancel_requested = false
        AND available_at <= now()
        AND ((status = 'QUEUED' AND (lease_expires_at IS NULL OR lease_expires_at < now()))
         OR (status IN ('COLLECTING','ANALYZING','NARRATING') AND lease_expires_at < now()))
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs AS j
SET lease_owner = %s,
    lease_expires_at = now() + make_interval(secs => %s),
    attempt = CASE WHEN j.status <> 'QUEUED' THEN j.attempt + 1 ELSE j.attempt END,
    status = CASE WHEN j.status <> 'QUEUED' THEN 'QUEUED'
                  ELSE j.status END,
    updated_at = now()
FROM candidate, queries q
WHERE j.job_id = candidate.job_id AND q.query_id = j.query_id
RETURNING {JOB_COLUMNS}
"""
_HEARTBEAT = """
UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
WHERE job_id = %s AND lease_owner = %s AND status IN ('COLLECTING','ANALYZING','NARRATING','QUEUED')
RETURNING cancel_requested
"""
_TRANSITION = """
UPDATE jobs
SET status = %s,
    started_at = CASE WHEN %s = 'COLLECTING' AND started_at IS NULL THEN now() ELSE started_at END,
    finished_at = CASE WHEN %s IN ('COMPLETED','PARTIAL','FAILED','CANCELLED') THEN now() ELSE finished_at END,
    lease_owner = CASE WHEN %s IN ('COMPLETED','PARTIAL','FAILED','CANCELLED','QUEUED')
                       THEN NULL ELSE lease_owner END,
    lease_expires_at = CASE WHEN %s IN ('COMPLETED','PARTIAL','FAILED','CANCELLED','QUEUED')
                            THEN NULL ELSE lease_expires_at END,
    error_code = COALESCE(%s, error_code),
    error_message = COALESCE(%s, error_message),
    updated_at = now()
WHERE job_id = %s AND (lease_owner = %s OR lease_owner IS NULL)
RETURNING job_id
"""
_SET_REFERENCES = """
UPDATE jobs
SET collection_id = COALESCE(%s::uuid, collection_id),
    analysis_id = COALESCE(%s::uuid, analysis_id),
    updated_at = now()
WHERE job_id = %s
"""
_REQUEST_CANCEL = """
UPDATE jobs
SET cancel_requested = true,
    status = CASE WHEN status = 'QUEUED' THEN 'CANCELLED' ELSE status END,
    error_code = CASE WHEN status = 'QUEUED' THEN 'CANCELLED_BY_USER' ELSE error_code END,
    error_message = CASE WHEN status = 'QUEUED' THEN 'задание отменено пользователем' ELSE error_message END,
    finished_at = CASE WHEN status = 'QUEUED' THEN now() ELSE finished_at END,
    lease_owner = CASE WHEN status = 'QUEUED' THEN NULL ELSE lease_owner END,
    lease_expires_at = CASE WHEN status = 'QUEUED' THEN NULL ELSE lease_expires_at END,
    updated_at = now()
WHERE job_id = %s
RETURNING job_id
"""
_RELEASE_EXPIRED = """
UPDATE jobs
SET status = 'QUEUED', lease_owner = NULL, lease_expires_at = NULL,
    attempt = attempt + 1, updated_at = now()
WHERE status IN ('COLLECTING','ANALYZING','NARRATING') AND lease_expires_at < %s AND attempt < 3
RETURNING job_id
"""
_FAIL_EXHAUSTED = """
UPDATE jobs
SET status = 'FAILED', lease_owner = NULL, lease_expires_at = NULL, finished_at = now(),
    error_code = 'LEASE_EXPIRED_MAX_ATTEMPTS',
    error_message = 'аренда истекала максимальное число раз, задание прекращено',
    updated_at = now()
WHERE status IN ('COLLECTING','ANALYZING','NARRATING') AND lease_expires_at < %s AND attempt >= 3
RETURNING job_id
"""
_COUNT_POSTPONEMENTS = (
    "SELECT count(*) AS total FROM job_events WHERE job_id = %s AND detail LIKE 'POSTPONED:%%'"
)
_POSTPONE = (
    "UPDATE jobs SET available_at = now() + make_interval(secs => %s), updated_at = now() "
    "WHERE job_id = %s"
)


class PostgresJobRepository:
    """Реализация порта `JobRepository` на асинхронном psycopg 3."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def insert(
        self, query: Query, job: Job, idempotency_key: str, request_hash: str, expires_at: datetime
    ) -> Job:
        """Одной транзакцией: запрос, задание, ключ идемпотентности и событие перехода."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _INSERT_QUERY,
                (
                    query.query_id,
                    query.text,
                    query.normalized_text,
                    query.requested_top_n,
                    query.client_ip_hash,
                ),
            )
            await cur.execute(_INSERT_JOB, (job.job_id, query.query_id))
            await cur.execute(_INSERT_KEY, (idempotency_key, request_hash, job.job_id, expires_at))
            await cur.execute(_INSERT_EVENT, (job.job_id, None, "QUEUED", None, None))
            await cur.execute(_SELECT_JOB, (job.job_id,))
            row = await cur.fetchone()
        return row_to_job(dict(row))

    async def get(self, job_id: str) -> Job | None:
        """Задание по идентификатору."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_JOB, (job_id,))
            row = await cur.fetchone()
        return row_to_job(dict(row)) if row else None

    async def list(
        self, *, limit: int, cursor: tuple[datetime, str] | None, status: JobStatus | None
    ) -> list[Job]:
        """Страница заданий по убыванию времени создания."""
        conditions = []
        params: list[Any] = []
        if status is not None:
            conditions.append("j.status = %s")
            params.append(status.value)
        if cursor is not None:
            conditions.append("(j.created_at, j.job_id) < (%s, %s)")
            params.extend([cursor[0], cursor[1]])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        sql = (
            f"SELECT {JOB_COLUMNS} FROM jobs j JOIN queries q ON q.query_id = j.query_id "
            f"{where} ORDER BY j.created_at DESC, j.job_id DESC LIMIT %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        return [row_to_job(dict(row)) for row in rows]

    async def count_pending(self) -> int:
        """Число незавершённых заданий."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COUNT_PENDING)
            row = await cur.fetchone()
        return int(row["total"]) if row else 0

    async def claim_next(self, owner: str, lease_seconds: int, now: datetime) -> Job | None:
        """Захват задания очереди или задания с истёкшей арендой."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_CLAIM, (owner, lease_seconds))
            row = await cur.fetchone()
            if row is None:
                return None
            job = row_to_job(dict(row))
            await cur.execute(_INSERT_EVENT, (job.job_id, job.status.value, "QUEUED", owner, "CLAIMED"))
        return job

    async def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> tuple[bool, bool]:
        """Продление аренды; отсутствие строки означает потерю аренды."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HEARTBEAT, (lease_seconds, job_id, owner))
            row = await cur.fetchone()
        if row is None:
            return False, False
        return True, bool(row["cancel_requested"])

    async def transition(
        self, job: Job, target: JobStatus, worker_id: str, detail: str = ""
    ) -> None:
        """Сохраняет переход статуса вместе с записью в журнал событий."""
        previous = job.status
        job.transition(target, job.finished_at or job.created_at or _now())
        error_code = job.error_code or None
        error_message = detail or job.error_message or None
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _TRANSITION,
                (
                    target.value,
                    target.value,
                    target.value,
                    target.value,
                    target.value,
                    error_code,
                    error_message,
                    job.job_id,
                    worker_id,
                ),
            )
            await cur.execute(
                _INSERT_EVENT, (job.job_id, previous.value, target.value, worker_id, detail or None)
            )

    async def set_references(
        self, job_id: str, collection_id: str = "", analysis_id: str = ""
    ) -> None:
        """Сохраняет идентификаторы коллекции и анализа."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SET_REFERENCES, (collection_id or None, analysis_id or None, job_id))

    async def request_cancel(self, job_id: str) -> Job | None:
        """Ставит признак отмены."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_REQUEST_CANCEL, (job_id,))
            if await cur.fetchone() is None:
                return None
            await cur.execute(_SELECT_JOB, (job_id,))
            row = await cur.fetchone()
        return row_to_job(dict(row)) if row else None

    async def release_expired_leases(self, now: datetime) -> int:
        """Возврат заданий с истёкшей арендой; исчерпавшие попытки завершаются отказом."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(_RELEASE_EXPIRED, (now,))
            released = await cur.fetchall()
            await cur.execute(_FAIL_EXHAUSTED, (now,))
            failed = await cur.fetchall()
        return len(released) + len(failed)

    async def postpone(self, job: Job, seconds: int, worker_id: str, reason: str) -> bool:
        """Откладывает задание без увеличения попытки; False при исчерпании лимита откладываний."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COUNT_POSTPONEMENTS, (job.job_id,))
            row = await cur.fetchone()
        if row and int(row["total"]) >= MAX_POSTPONEMENTS:
            return False
        await self.transition(job, JobStatus.QUEUED, worker_id, f"POSTPONED:{reason}:{seconds}s")
        # Задержка до следующего захвата: без неё воркер забирает задание немедленно и лимит
        # откладываний сгорает за секунды, не пережив перезапуска соседнего сервиса.
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_POSTPONE, (seconds, job.job_id))
        return True


def _now() -> datetime:
    """Текущее время UTC (используется при переходах в памяти)."""
    from datetime import UTC  # noqa: PLC0415 - локальный импорт ради краткости модуля

    return datetime.now(UTC)
