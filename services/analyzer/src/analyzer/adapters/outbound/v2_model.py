"""Линейная модель v2 (последовательное обучение A → B): JSON-артефакт без pickle.

Вклад признака в логит — вес × значение (признаки ограничены по построению, скейлер не нужен).
Калибровка Платта хранится двумя числами; стадия и тренд в v2 не предсказываются.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from analyzer.domain.features_v2 import Projection

V2_FAMILY = "logreg_seq_laplace"


@dataclass(frozen=True, slots=True)
class V2Spec:
    """Параметры вычисления признаков v2, привязанные к артефакту."""

    projection: Projection
    glossary: dict[str, str] = field(default_factory=dict)
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "
    lineage: dict[str, Any] = field(default_factory=dict)


class LinearV2Classifier:
    """Реализация порта `ClassifierModel` для артефакта v2."""

    def __init__(
        self,
        feature_names: Sequence[str],
        weights: Sequence[float],
        intercept: float,
        calibration: tuple[float, float] = (1.0, 0.0),
    ) -> None:
        self._names = tuple(feature_names)
        self._weights = np.asarray(weights, dtype=np.float64)
        if self._weights.shape != (len(self._names),):
            raise ValueError("число весов не совпадает с числом признаков")
        self._intercept = float(intercept)
        self._slope, self._offset = (float(calibration[0]), float(calibration[1]))

    @property
    def feature_names(self) -> tuple[str, ...]:
        """Имена признаков в порядке вектора модели."""
        return self._names

    def raw_scores(self, features: np.ndarray) -> np.ndarray:
        """Логит до калибровки."""
        return np.asarray(features, dtype=np.float64) @ self._weights + self._intercept

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность «релевантный слабый сигнал»."""
        logits = self._slope * self.raw_scores(features) + self._offset
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))

    def contributions(self, features: np.ndarray) -> np.ndarray:
        """Вклады признаков в некалиброванный логит."""
        return np.asarray(features, dtype=np.float64) * self._weights

    def predict_stage(self, features: np.ndarray) -> list[int | None]:
        """Стадия в v2 не предсказывается."""
        return [None] * int(np.asarray(features).shape[0])

    def predict_trend(self, features: np.ndarray) -> list[int | None]:
        """Тренд в v2 не предсказывается."""
        return [None] * int(np.asarray(features).shape[0])
