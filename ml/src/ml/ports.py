"""Порты ML-конвейера (`typing.Protocol`): collector, эмбеддер, трекинг экспериментов, хранилище."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np


class CollectorClient(Protocol):
    """Клиент collector для обогащения обучающих строк (режим ENRICHMENT)."""

    def enrich(self, idempotency_key: str, title_ru: str, title_en: str) -> dict[str, Any]:
        """Собирает коллекцию по названию технологии и возвращает документы и энциклопедию."""


class Embedder(Protocol):
    """Модель эмбеддингов; контракт совпадает с портом analyzer."""

    @property
    def model_name(self) -> str:
        """Идентификатор модели, который попадёт в манифест."""

    @property
    def dims(self) -> int:
        """Размерность вектора."""

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов с префиксом e5, L2-нормализованные."""


class ExperimentTracker(Protocol):
    """Трекинг экспериментов (MLflow или заглушка)."""

    def start_run(self, name: str) -> None:
        """Начинает запуск эксперимента."""

    def log_params(self, params: dict[str, Any]) -> None:
        """Логирует параметры."""

    def log_metrics(self, metrics: dict[str, float]) -> None:
        """Логирует метрики."""

    def log_artifact(self, path: Path) -> None:
        """Прикладывает файл к запуску."""

    def end_run(self) -> None:
        """Завершает запуск."""


class ModelStore(Protocol):
    """Каталог артефактов модели."""

    def export(self, version_id: str, artifacts: dict[str, Any], manifest: dict[str, Any]) -> Path:
        """Пишет артефакты и манифест, обновляет `active`, возвращает каталог версии."""
