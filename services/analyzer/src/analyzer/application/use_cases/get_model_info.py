"""Сценарий GetModelInfo: сведения об активной модели."""

from __future__ import annotations

from analyzer.application.active_model import ActiveModelHolder
from analyzer.domain.entities import ModelVersion


class GetModelInfo:
    """Отдаёт метаданные и метрики активной модели вместе с порядком признаков реестра."""

    def __init__(self, active_model: ActiveModelHolder) -> None:
        self._active_model = active_model

    def execute(self) -> tuple[ModelVersion, tuple[str, ...]]:
        """Версия модели и имена признаков в порядке реестра (все 25)."""
        bundle = self._active_model.get()
        return bundle.version, bundle.registry.names
