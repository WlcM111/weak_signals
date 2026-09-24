"""Сценарии чтения снимка результата: полный результат и отдельный элемент."""

from __future__ import annotations

from orchestrator.application.dto import ResultsView
from orchestrator.application.ports import JobRepository, ResultRepository
from orchestrator.domain.entities import ResultItem
from orchestrator.domain.errors import NotFoundError, PreconditionFailedError
from orchestrator.domain.values import JobStatus

READABLE_STATUSES = frozenset(
    {JobStatus.NARRATING, JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.CANCELLED}
)


class GetResults:
    """Снимок результата задания: элементы по рангу, исключённые кандидаты и статистика."""

    def __init__(self, jobs: JobRepository, results: ResultRepository) -> None:
        self._jobs = jobs
        self._results = results

    async def execute(self, job_id: str) -> ResultsView:
        """До стадии NARRATING результата ещё нет — 409 (прогрессивная выдача начинается с неё)."""
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"задание {job_id} не найдено")
        if job.status not in READABLE_STATUSES:
            raise PreconditionFailedError(
                f"результат ещё не формируется: задание в статусе {job.status.value}",
                "RESULTS_NOT_READY",
            )
        items, excluded, stats = await self._results.get_results(job_id)
        return ResultsView(
            job=job,
            query_text=job.query_text,
            items=tuple(items),
            excluded=tuple(excluded),
            stats=stats,
        )


class GetResultItem:
    """Элемент выдачи целиком: нарратив, все признаки и источники."""

    def __init__(self, results: ResultRepository, jobs: JobRepository) -> None:
        self._results = results
        self._jobs = jobs

    async def execute(self, item_id: str) -> tuple[ResultItem, str]:
        """Элемент и текст запроса задания, которому он принадлежит."""
        item = await self._results.get_item(item_id)
        if item is None:
            raise NotFoundError(f"элемент {item_id} не найден")
        job = await self._jobs.get(item.job_id)
        return item, (job.query_text if job else "")
