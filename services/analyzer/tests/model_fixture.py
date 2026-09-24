"""Сборка настоящего артефакта модели для тестов (маленькая логистическая регрессия).

Фикстура повторяет то, что кладёт в `model-store` trainer: обученные `scaler/classifier/calibrator`,
центроиды, `rules.json` и манифест с sha256 — поэтому тесты проверяют реальный путь загрузки,
а не заглушку.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

MODEL_VERSION_ID = "wsclf-2026.09.15-1"
DATASET_VERSION = "ds-2026.09.15-v1"
EMBEDDING_MODEL = "intfloat/multilingual-e5-base"
THRESHOLD = 0.5
EMBEDDING_DIMS = 8


def build_model_store(
    root: Path, feature_names: tuple[str, ...], *, corrupt: bool = False, dims: int = EMBEDDING_DIMS
) -> Path:
    """Создаёт каталог `model-store/active` с рабочими артефактами; `corrupt` ломает sha256.

    `dims` задаёт размерность эталонных центроидов: она должна совпадать с эмбеддером теста.
    """
    directory = root / "active"
    directory.mkdir(parents=True, exist_ok=True)
    scaler, classifier, calibrator = _train(len(feature_names))
    joblib.dump(scaler, directory / "scaler.joblib")
    joblib.dump(classifier, directory / "classifier.joblib")
    joblib.dump(calibrator, directory / "calibrator.joblib")
    np.savez(
        directory / "centroids.npz",
        weak=_unit_vector(dims, seed=1),
        mature=_unit_vector(dims, seed=2),
    )
    (directory / "rules.json").write_text(
        json.dumps(
            {
                "version": 1,
                "thresholds": {"min_query_similarity": 0.30, "wiki_pageviews_min": 20000},
                "feature_defaults": {"recency_median_days": 400.0, "first_seen_years_ago": 2.0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    files = {
        "classifier": "classifier.joblib",
        "scaler": "scaler.joblib",
        "calibrator": "calibrator.joblib",
        "centroids": "centroids.npz",
    }
    checksums = {name: _sha256(directory / path) for name, path in files.items()}
    checksums["rules.json"] = _sha256(directory / "rules.json")
    if corrupt:
        checksums["classifier"] = "0" * 64
    manifest = {
        "model_version_id": MODEL_VERSION_ID,
        "model_family": "logreg_elasticnet",
        "feature_schema_version": "v1",
        "embedding_model": EMBEDDING_MODEL,
        "dataset_version": DATASET_VERSION,
        "artifact_files": {**files, "sha256": checksums},
        "threshold": THRESHOLD,
        "metrics": {
            "test": {
                "accuracy": 0.84,
                "precision": 0.82,
                "recall": 0.80,
                "f1": 0.81,
                "roc_auc": 0.88,
                "n": 44,
            },
            "cv": {
                "protocol": "holdout_test_20pct + repeated_stratified_5x5cv",
                "f1_mean": 0.80,
                "f1_std": 0.04,
                "accuracy_mean": 0.82,
                "accuracy_std": 0.03,
            },
        },
        "trained_at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC).isoformat(),
        "git_commit": "0" * 40,
        "stage_model_present": False,
        "trend_model_present": False,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def _train(feature_count: int) -> tuple[StandardScaler, LogisticRegression, object]:
    """Обучает крошечную модель на синтетических данных: первый признак — «за слабый сигнал»."""
    generator = np.random.default_rng(20260915)
    positives = generator.normal(loc=1.0, scale=0.5, size=(60, feature_count))
    negatives = generator.normal(loc=-1.0, scale=0.5, size=(60, feature_count))
    features = np.vstack([positives, negatives])
    labels = np.array([1] * 60 + [0] * 60)
    scaler = StandardScaler().fit(features)
    scaled = scaler.transform(features)
    classifier = LogisticRegression(max_iter=1000).fit(scaled, labels)
    calibrator = _SigmoidCalibrator().fit(classifier.decision_function(scaled), labels)
    return scaler, classifier, calibrator


class _SigmoidCalibrator:
    """Калибратор Платта: логистическая регрессия на сырых оценках классификатора."""

    def __init__(self) -> None:
        self._model = LogisticRegression(max_iter=1000)

    def fit(self, raw_scores: np.ndarray, labels: np.ndarray) -> _SigmoidCalibrator:
        """Обучает калибратор на одномерных оценках."""
        self._model.fit(np.asarray(raw_scores).reshape(-1, 1), labels)
        return self

    @property
    def n_features_in_(self) -> int:
        """Калибратор принимает один столбец — сырую оценку."""
        return 1

    def predict_proba(self, raw_scores: np.ndarray) -> np.ndarray:
        """Вероятности классов по сырым оценкам."""
        return self._model.predict_proba(np.asarray(raw_scores).reshape(-1, 1))


def _unit_vector(dims: int, seed: int) -> np.ndarray:
    """Детерминированный единичный вектор для эталонного центроида."""
    generator = np.random.default_rng(seed)
    vector = generator.normal(size=dims).astype(np.float32)
    return vector / np.linalg.norm(vector)


def _sha256(path: Path) -> str:
    """Контрольная сумма файла."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
