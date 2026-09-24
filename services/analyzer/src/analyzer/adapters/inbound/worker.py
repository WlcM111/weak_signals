"""Фоновые потоки analyzer: воркер анализов и снятие истёкших аренд (§9 HANDOFF)."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyzer.application.ports import AnalysisRepository
from analyzer.application.use_cases.run_analysis import RunAnalysis
from analyzer.domain.errors import LeaseLost
from analyzer.domain.values import AnalysisErrorCode, AnalysisStats, OperationStatus
from ws_common.logging import get_logger

POLL_INTERVAL_SECONDS = 1.0
LEASE_SWEEP_INTERVAL_SECONDS = 30.0


class AnalysisWorker:
    """Опрашивает очередь анализов и выполняет их по одному."""

    def __init__(
        self,
        analyses: AnalysisRepository,
        run_analysis: RunAnalysis,
        owner: str,
        lease_seconds: int,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._analyses = analyses
        self._run_analysis = run_analysis
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval
        self._log = get_logger("analyzer.worker")

    def run_forever(self, stop: threading.Event) -> None:
        """Цикл воркера до сигнала остановки; сбой БД при захвате не завершает поток воркера."""
        while not stop.is_set():
            try:
                processed = self.run_once()
            except Exception as error:  # noqa: BLE001 - временный сбой БД: повтор после паузы
                self._log.error("worker.claim_failed", error=str(error))
                processed = False
            if not processed:
                stop.wait(self._poll_interval)

    def run_once(self) -> bool:
        """Обрабатывает один анализ, если он есть в очереди."""
        analysis = self._analyses.claim_next(self._owner, self._lease_seconds)
        if analysis is None:
            return False
        try:
            self._run_analysis.execute(analysis, self._owner)
        except LeaseLost:
            self._log.warning("analysis.lease_lost", analysis_id=analysis.analysis_id)
        except Exception as error:  # noqa: BLE001 - непредвиденная ошибка: анализ завершается отказом
            self._log.error(
                "analysis.failed_unexpectedly", analysis_id=analysis.analysis_id, error=str(error)
            )
            self._fail_poisoned(analysis.analysis_id, str(error))
        return True

    def _fail_poisoned(self, analysis_id: str, reason: str) -> None:
        """Завершает анализ отказом после непредвиденной ошибки.

        Без этого аренда истекала, анализ возвращался в очередь и падал на том же месте бесконечно,
        а задание orchestrator ждало его до тайм-аута стадии (300 с).
        """
        try:
            self._analyses.finish(
                analysis_id,
                self._owner,
                OperationStatus.FAILED,
                stats=AnalysisStats(),
                error_code=AnalysisErrorCode.INTERNAL_ERROR.value,
                error_message=f"непредвиденная ошибка анализа: {reason}"[:500],
                finished_at=datetime.now(UTC),
            )
        except Exception as error:  # noqa: BLE001 - запись отказа не должна ронять воркер
            self._log.error("analysis.fail_record_failed", analysis_id=analysis_id, error=str(error))


class LeaseSweeper:
    """Возвращает в очередь анализы с истёкшей арендой."""

    def __init__(
        self, analyses: AnalysisRepository, interval: float = LEASE_SWEEP_INTERVAL_SECONDS
    ) -> None:
        self._analyses = analyses
        self._interval = interval
        self._log = get_logger("analyzer.maintenance")

    def run_forever(self, stop: threading.Event) -> None:
        """Периодическая проверка аренд до сигнала остановки."""
        while not stop.is_set():
            try:
                released = self._analyses.release_expired_leases()
                if released:
                    self._log.warning("analysis.leases_released", count=released)
            except Exception as error:  # noqa: BLE001 - фоновая задача не должна останавливать сервис
                self._log.error("maintenance.failed", error=str(error))
            stop.wait(self._interval)


MODEL_WATCH_INTERVAL_SECONDS = 15.0
MODEL_RELOAD_ATTEMPTS = 3


class ModelWatcher:
    """Подхватывает появившуюся или обновлённую модель в `model-store/active` без перезапуска сервиса.

    Раньше модель читалась один раз при старте: после `trainer train` требовался ручной
    `docker compose restart analyzer`, а сервис, запущенный до обучения, навсегда оставался не готов.
    """

    def __init__(
        self,
        activate: Callable[[], Any],
        is_loaded: Callable[[], bool],
        manifest_path: Path,
        interval: float = MODEL_WATCH_INTERVAL_SECONDS,
    ) -> None:
        self._activate = activate
        self._is_loaded = is_loaded
        self._manifest_path = Path(manifest_path)
        self._interval = interval
        self._log = get_logger("analyzer.model_watcher")
        self._seen = ""
        self._failed = ""
        self._attempts = 0

    def fingerprint(self) -> str:
        """sha256 манифеста активной модели; пустая строка, если манифеста нет."""
        try:
            return hashlib.sha256(self._manifest_path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def poll_once(self) -> bool:
        """Одна сверка манифеста; True, если модель была активирована на этом шаге."""
        current = self.fingerprint()
        if not current or (current == self._seen and self._is_loaded()):
            return False
        if current == self._failed and self._attempts >= MODEL_RELOAD_ATTEMPTS:
            return False  # этот манифест уже не удалось загрузить: ждём следующего обучения
        try:
            self._activate()
        except Exception as error:  # noqa: BLE001 - прежняя модель продолжает работать
            self._attempts = self._attempts + 1 if current == self._failed else 1
            self._failed = current
            self._log.warning("model.reload_failed", attempt=self._attempts, error=str(error)[:300])
            return False
        self._seen, self._failed, self._attempts = current, "", 0
        self._log.info("model.reloaded", manifest_sha256=current)
        return True

    def run_forever(self, stop: threading.Event) -> None:
        """Раз в `interval` секунд сверяет манифест и при изменении активирует модель."""
        if self._is_loaded():
            self._seen = self.fingerprint()  # модель, загруженная при старте, повторно не активируется
        while not stop.is_set():
            stop.wait(self._interval)
            if not stop.is_set():
                self.poll_once()
