"""Сценарий SubmitQuery: идемпотентный приём запроса пользователя (§7 HANDOFF)."""

from __future__ import annotations

from datetime import timedelta

from orchestrator.application.dto import SubmitQueryCommand, SubmitQueryResult
from orchestrator.application.ports import IdempotencyRepository, JobRepository
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.errors import IdempotencyConflict, QueueFull
from ws_common.clock import Clock
from ws_common.ids import uuid7
from ws_common.logging import get_logger


class SubmitQuery:
    """Создаёт задание либо возвращает существующее по ключу идемпотентности."""

    def __init__(
        self,
        jobs: JobRepository,
        idempotency: IdempotencyRepository,
        clock: Clock,
        max_pending: int,
        idempotency_ttl_hours: int = 24,
    ) -> None:
        self._jobs = jobs
        self._idempotency = idempotency
        self._clock = clock
        self._max_pending = max_pending
        self._ttl_hours = idempotency_ttl_hours
        self._log = get_logger("orchestrator.submit_query")

    async def execute(self, command: SubmitQueryCommand) -> SubmitQueryResult:
        """Повтор с тем же ключом и телом возвращает тот же job_id; с другим телом — конфликт."""
        existing = await self._idempotency.get(command.idempotency_key)
        if existing is not None:
            job_id, request_hash = existing
            if request_hash != command.request_hash:
                raise IdempotencyConflict(
                    "ключ идемпотентности уже использован с другим телом запроса"
                )
            job = await self._jobs.get(job_id)
            if job is not None:
                return SubmitQueryResult(
                    job_id=job.job_id, query_id=job.query_id, status=job.status.value, created=False
                )
        pending = await self._jobs.count_pending()
        if pending >= self._max_pending:
            raise QueueFull(f"очередь заданий заполнена ({pending} ≥ {self._max_pending})")
        query = Query.create(
            query_id=str(uuid7()),
            text=command.query_text,
            top_n=command.top_n,
            client_ip_hash=command.client_ip_hash,
        )
        job = Job(
            job_id=str(uuid7()),
            query_id=query.query_id,
            query_text=query.text,
            requested_top_n=query.requested_top_n,
        )
        stored = await self._jobs.insert(
            query,
            job,
            command.idempotency_key,
            command.request_hash,
            self._clock.now() + timedelta(hours=self._ttl_hours),
        )
        self._log.info("job.accepted", job_id=stored.job_id, top_n=query.requested_top_n)
        return SubmitQueryResult(
            job_id=stored.job_id, query_id=stored.query_id, status=stored.status.value, created=True
        )
