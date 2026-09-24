"""Сценарии чтения заданий: одно задание и постраничный список."""

from __future__ import annotations

from orchestrator.application.dto import JobPage, JobView
from orchestrator.application.ports import JobRepository, ResultRepository
from orchestrator.application.validation import encode_cursor
from orchestrator.domain.errors import NotFoundError
from orchestrator.domain.values import JobProgress, JobStatus


class GetJob:
    """Состояние задания вместе с прогрессом выполнения."""

    def __init__(self, jobs: JobRepository, results: ResultRepository) -> None:
        self._jobs = jobs
        self._results = results

    async def execute(self, job_id: str) -> JobView:
        """Задание и прогресс; NOT_FOUND, если задания нет."""
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"задание {job_id} не найдено")
        progress = await self._results.progress(job_id) or JobProgress(stage=job.status)
        return JobView(job=job, progress=progress)


class ListJobs:
    """Список заданий по убыванию времени создания с keyset-пагинацией."""

    def __init__(self, jobs: JobRepository, results: ResultRepository) -> None:
        self._jobs = jobs
        self._results = results

    async def execute(
        self, *, limit: int, cursor: tuple | None, status: JobStatus | None
    ) -> JobPage:
        """Страница заданий; курсор непустой, только если страница заполнена целиком."""
        rows = await self._jobs.list(limit=limit, cursor=cursor, status=status)
        views: list[JobView] = []
        for job in rows:
            progress = await self._results.progress(job.job_id) or JobProgress(stage=job.status)
            views.append(JobView(job=job, progress=progress))
        next_cursor = ""
        if len(rows) == limit and rows and rows[-1].created_at is not None:
            next_cursor = encode_cursor(rows[-1].created_at, rows[-1].job_id)
        return JobPage(items=tuple(views), next_cursor=next_cursor)
