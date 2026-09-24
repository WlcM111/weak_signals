"""Сценарий CancelAnalysis: кооперативная идемпотентная отмена."""

from __future__ import annotations

from analyzer.application.ports import AnalysisRepository
from analyzer.domain.errors import NotFoundError
from analyzer.domain.values import OperationStatus
from ws_common.logging import get_logger


class CancelAnalysis:
    """Ставит признак отмены; повторный вызов возвращает фактический статус без ошибки."""

    def __init__(self, analyses: AnalysisRepository) -> None:
        self._analyses = analyses
        self._log = get_logger("analyzer.cancel_analysis")

    def execute(self, analysis_id: str, reason: str) -> OperationStatus:
        """Возвращает CANCELLED либо уже достигнутый терминальный статус."""
        status = self._analyses.request_cancel(analysis_id)
        if status is None:
            raise NotFoundError(f"анализ {analysis_id} не найден")
        self._log.info(
            "analysis.cancel_requested", analysis_id=analysis_id, status=status.value, reason=reason
        )
        return status
