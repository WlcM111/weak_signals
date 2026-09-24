"""Обёртка артефактов scikit-learn: калиброванная вероятность и вклады признаков.

Вклад признака в логит: для линейной модели — коэффициент × стандартизованное значение;
для LightGBM — вклады `pred_contrib` (SHAP), которые библиотека считает сама, без пакета shap.
"""

from __future__ import annotations

from typing import Any

import numpy as np

LOGREG_FAMILY = "logreg_elasticnet"
LIGHTGBM_FAMILY = "lightgbm"


class SklearnClassifier:
    """Реализация порта `ClassifierModel` поверх joblib-артефактов trainer-а."""

    def __init__(
        self,
        scaler: Any,
        classifier: Any,
        calibrator: Any,
        feature_names: tuple[str, ...],
        model_family: str,
        stage_model: Any = None,
        trend_model: Any = None,
    ) -> None:
        self._scaler = scaler
        self._classifier = classifier
        self._calibrator = calibrator
        self._feature_names = feature_names
        self._model_family = model_family
        self._stage_model = stage_model
        self._trend_model = trend_model

    @property
    def feature_names(self) -> tuple[str, ...]:
        """Имена признаков в порядке вектора модели."""
        return self._feature_names

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность слабого сигнала."""
        scaled = self._scale(features)
        raw = self._raw_scores(scaled)
        return np.clip(self._calibrate(raw, scaled), 0.0, 1.0)

    def contributions(self, features: np.ndarray) -> np.ndarray:
        """Вклады признаков в логит, в порядке `feature_names`."""
        scaled = self._scale(features)
        if self._model_family == LIGHTGBM_FAMILY and hasattr(self._classifier, "predict"):
            try:
                contributions = np.asarray(
                    self._classifier.predict(scaled, pred_contrib=True), dtype=np.float64
                )
                return contributions[:, :-1]  # последний столбец — базовое значение
            except TypeError:
                pass  # не LightGBM-совместимый объект: используется линейное приближение
        coefficients = self._coefficients()
        if coefficients is None:
            return np.zeros_like(scaled)
        return scaled * coefficients

    def predict_stage(self, features: np.ndarray) -> list[int | None]:
        """Предсказанная стадия 1..4 или None, если вспомогательной модели нет."""
        return self._predict_auxiliary(self._stage_model, features, low=1, high=4)

    def predict_trend(self, features: np.ndarray) -> list[int | None]:
        """Предсказанный тренд 1..3 или None, если вспомогательной модели нет."""
        return self._predict_auxiliary(self._trend_model, features, low=1, high=3)

    def _predict_auxiliary(
        self, model: Any, features: np.ndarray, low: int, high: int
    ) -> list[int | None]:
        """Предсказание вспомогательной модели с ограничением диапазона."""
        rows = int(np.asarray(features).shape[0])
        if model is None:
            return [None] * rows
        predictions = np.asarray(model.predict(self._scale(features)))
        return [int(min(high, max(low, round(float(value))))) for value in predictions]

    def _scale(self, features: np.ndarray) -> np.ndarray:
        """Стандартизация вектора признаков."""
        matrix = np.asarray(features, dtype=np.float64)
        if self._scaler is None:
            return matrix
        return np.asarray(self._scaler.transform(matrix), dtype=np.float64)

    def _raw_scores(self, scaled: np.ndarray) -> np.ndarray:
        """Сырые оценки классификатора до калибровки."""
        if hasattr(self._classifier, "decision_function"):
            return np.asarray(self._classifier.decision_function(scaled), dtype=np.float64).ravel()
        proba = np.asarray(self._classifier.predict_proba(scaled), dtype=np.float64)
        return proba[:, 1]

    def _calibrate(self, raw: np.ndarray, scaled: np.ndarray) -> np.ndarray:
        """Применяет калибратор Платта; поддерживаются три формы артефакта."""
        if self._calibrator is None:
            return raw if _looks_like_probability(raw) else _sigmoid(raw)
        expected = getattr(self._calibrator, "n_features_in_", None)
        if expected == scaled.shape[1] and hasattr(self._calibrator, "predict_proba"):
            return np.asarray(self._calibrator.predict_proba(scaled), dtype=np.float64)[:, 1]
        column = raw.reshape(-1, 1)
        if hasattr(self._calibrator, "predict_proba"):
            return np.asarray(self._calibrator.predict_proba(column), dtype=np.float64)[:, 1]
        return np.asarray(self._calibrator.predict(raw), dtype=np.float64).ravel()

    def _coefficients(self) -> np.ndarray | None:
        """Коэффициенты линейной модели, если они есть."""
        coefficients = getattr(self._classifier, "coef_", None)
        if coefficients is None:
            return None
        array = np.asarray(coefficients, dtype=np.float64)
        return array[0] if array.ndim == 2 else array


def _sigmoid(values: np.ndarray) -> np.ndarray:
    """Логистическая функция для перевода логита в вероятность."""
    return 1.0 / (1.0 + np.exp(-np.clip(values, -30.0, 30.0)))


def _looks_like_probability(values: np.ndarray) -> bool:
    """Похожи ли значения на вероятности (для артефакта без калибратора)."""
    return bool(values.size == 0 or (values.min() >= 0.0 and values.max() <= 1.0))
