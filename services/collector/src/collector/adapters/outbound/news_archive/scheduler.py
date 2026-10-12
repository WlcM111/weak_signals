"""Расписание фоновых загрузок архивов внутри collector (этап 5.0, аудит раунда 3 N15).

Без расписания свежесть архива новостей, корпуса arXiv и текста статей зависела от ручного запуска команд `archive`.
Задачи выполняются по одной, последовательно, с паузой между проверками; момент последнего запуска хранится в
collector.archive_schedule, поэтому перезапуск сервиса не повторяет только что выполненную задачу.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from ws_common.clock import Clock


class ArchiveScheduler:
    def __init__(self, runner: Callable[[str], Awaitable[str]], intervals: Mapping[str, float], store, clock: Clock,  # noqa: ANN001
                 log, check_seconds: float = 300.0) -> None:  # noqa: ANN001
        self._runner, self._intervals, self._store, self._clock = runner, dict(intervals), store, clock
        self._log, self._check = log, check_seconds

    async def tick(self) -> list[str]:
        """Выполняет задачи, срок которых наступил; возвращает их имена."""
        done = []
        for job, hours in self._intervals.items():
            if hours <= 0:
                continue
            last = await self._store.last_job_run(job)
            if last is not None and self._clock.now() - last < timedelta(hours=hours):
                continue
            try:
                detail, status = await self._runner(job), "ok"
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — сбой задачи не останавливает расписание
                detail, status = str(error)[:500], "failed"
            await self._store.mark_job_run(job, status, detail)
            self._log.info("archive.schedule", job=job, status=status, detail=detail[:200])
            done.append(job)
        return done

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self._log.warning("archive.schedule_failed", error=str(error)[:200])
            await self._clock.sleep(self._check)
