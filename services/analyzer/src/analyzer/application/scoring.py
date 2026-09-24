"""Сборка вектора признаков, вызов модели и построение вкладов (общее для RunAnalysis и ScoreText)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from analyzer.application.dto import ModelBundle
from analyzer.domain.entities import FeatureContribution
from analyzer.domain.rules import direction_of
from analyzer.domain.values import FeatureDirection


def build_feature_matrix(bundle: ModelBundle, rows: Sequence[dict[str, float]]) -> np.ndarray:
    """Матрица признаков модели (n × 24) в порядке реестра с клиппингом по диапазонам."""
    if not rows:
        return np.zeros((0, len(bundle.registry.model_names)), dtype=np.float64)
    return np.asarray([bundle.registry.model_vector(values) for values in rows], dtype=np.float64)


def build_contributions(
    bundle: ModelBundle, values: dict[str, float], contributions_row: Sequence[float] | None
) -> tuple[FeatureContribution, ...]:
    """Все 25 признаков реестра с вкладами, отсортированные по модулю вклада убывающе.

    `emb_sim_query` в модель не подаётся (§12.7 ТЗ), поэтому его вклад всегда 0 и направление —
    нейтральное: признак участвует только в правиле OFF_TOPIC.
    """
    model_names = bundle.registry.model_names
    by_name = (
        dict(zip(model_names, contributions_row, strict=True)) if contributions_row is not None else {}
    )
    items: list[FeatureContribution] = []
    for spec in bundle.registry.features:
        value = spec.clip(values.get(spec.name, 0.0))
        contribution = float(by_name.get(spec.name, 0.0))
        items.append(
            FeatureContribution(
                feature_name=spec.name,
                value=value,
                contribution=contribution,
                label_ru=spec.label_ru,
                direction=direction_of(contribution) if spec.used_in_model else FeatureDirection.NEUTRAL,
            )
        )
    return tuple(sorted(items, key=lambda item: (-abs(item.contribution), item.feature_name)))


def score_rows(
    bundle: ModelBundle, rows: Sequence[dict[str, float]]
) -> tuple[np.ndarray, np.ndarray, list[int | None], list[int | None]]:
    """Вероятности, вклады и предсказания вспомогательных моделей для набора наблюдений."""
    matrix = build_feature_matrix(bundle, rows)
    if matrix.shape[0] == 0:
        empty = np.zeros((0,), dtype=np.float64)
        return empty, np.zeros((0, matrix.shape[1]), dtype=np.float64), [], []
    scores = np.clip(np.asarray(bundle.classifier.predict_proba(matrix), dtype=np.float64), 0.0, 1.0)
    contributions = np.asarray(bundle.classifier.contributions(matrix), dtype=np.float64)
    stages = bundle.classifier.predict_stage(matrix)
    trends = bundle.classifier.predict_trend(matrix)
    return scores, contributions, stages, trends


def merge_feature_values(*parts: dict[str, float]) -> dict[str, float]:
    """Объединяет группы признаков в один словарь значений."""
    merged: dict[str, float] = {}
    for part in parts:
        merged.update(part)
    return merged
