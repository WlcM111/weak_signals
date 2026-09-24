"""Сценарий CancelJob: кооперативная отмена задания."""

from __future__ import annotations

from orchestrator.application.dto import JobView
from orchestrator.application.ports import JobRepository, ResultRepository
from orchestrator.domain.entities import Job
from orchestrator.domain.errors import JobNotCancellable, NotFoundError
from orchestrator.domain.values import JobProgress
from ws_common.logging import get_logger


class CancelJob:
    """Ставит признак отмены; воркер прекращает работу на ближайшей проверке."""

    def __init__(self, jobs: JobRepository, results: ResultRepository) -> None:
        self._jobs = jobs
        self._results = results
        self._log = get_logger("orchestrator.cancel_job")

    async def execute(self, job_id: str) -> JobView:
        """Терминальное задание отменить нельзя — 409 `JOB_NOT_CANCELLABLE`."""
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"задание {job_id} не найдено")
        if job.is_terminal:
            raise JobNotCancellable(f"задание уже завершено со статусом {job.status.value}")
        updated: Job | None = await self._jobs.request_cancel(job_id)
        if updated is None:
            raise NotFoundError(f"задание {job_id} не найдено")
        self._log.info("job.cancelled", job_id=job_id, status=updated.status.value)
        progress = await self._results.progress(job_id) or JobProgress(stage=updated.status)
        return JobView(job=updated, progress=progress)
