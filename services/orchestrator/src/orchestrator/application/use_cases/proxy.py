"""Прокси-сценарии к analyzer: сведения о модели и прямой скоринг текста (§7 HANDOFF)."""

from __future__ import annotations

from orchestrator.application.dto import ModelInfoView, ScoreTextView
from orchestrator.application.ports import AnalyzerClient


class GetModelInfo:
    """Отдаёт метаданные активной модели интерфейсу без собственного кеша."""

    def __init__(self, analyzer: AnalyzerClient) -> None:
        self._analyzer = analyzer

    async def execute(self) -> ModelInfoView:
        """Сведения об активной модели."""
        return await self._analyzer.get_model_info()


class ScoreText:
    """Прямой скоринг описания технологии (страница методологии интерфейса)."""

    def __init__(self, analyzer: AnalyzerClient) -> None:
        self._analyzer = analyzer

    async def execute(self, title: str, description: str, with_enrichment: bool) -> ScoreTextView:
        """Проксирует запрос в analyzer без изменения решения и оценки."""
        return await self._analyzer.score_text(title, description, with_enrichment)
