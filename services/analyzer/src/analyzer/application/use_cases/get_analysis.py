"""Сценарий GetAnalysis: статус и статистика анализа."""

from __future__ import annotations

from analyzer.application.dto import AnalysisView
from analyzer.application.ports import AnalysisRepository
from analyzer.domain.errors import NotFoundError


class GetAnalysis:
    """Читает состояние анализа."""

    def __init__(self, analyses: AnalysisRepository) -> None:
        self._analyses = analyses

    def execute(self, analysis_id: str) -> AnalysisView:
        """Состояние анализа; NOT_FOUND, если его нет."""
        analysis = self._analyses.get(analysis_id)
        if analysis is None:
            raise NotFoundError(f"анализ {analysis_id} не найден")
        return AnalysisView(
            analysis_id=analysis.analysis_id,
            status=analysis.status,
            model_version_id=analysis.model_version_id,
            stats=analysis.stats,
            error_code=analysis.error_code,
            error_message=analysis.error_message,
            started_at=analysis.started_at,
            finished_at=analysis.finished_at,
        )
