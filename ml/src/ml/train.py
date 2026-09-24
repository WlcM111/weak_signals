"""Сценарий `train`: baselines, кросс-валидация, калибровка, порог и итоговые артефакты (§7.2).

Протокол оценки: стратифицированный holdout 20 % (используется ровно один раз, в самом конце),
на остальных 80 % — `RepeatedStratifiedKFold`. Эталонные центроиды `emb_sim_*` пересчитываются
внутри каждого фолда только по его обучающей части: иначе признак несёт информацию о метках
валидации. Порог выбирается на CV-предсказаниях, тест в выборе не участвует.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import RepeatedStratifiedKFold, StratifiedKFold, train_test_split
from sklearn.preprocessing import StandardScaler

from ml.dataset import TrainingRow
from ml.features import FeatureContext, build_matrix, centroids_from, row_features
from ws_common.logging import get_logger

PASSAGE_PREFIX = "passage: "
EMB_FEATURES = ("emb_sim_weak_centroid", "emb_sim_mature_centroid")
CALIBRATION_BINS = 10
BOOTSTRAP_SAMPLES = 1000
LR_GRID = tuple((c, ratio) for c in (0.1, 0.3, 1.0, 3.0) for ratio in (0.0, 0.5))
# scikit-learn 1.8 объявил параметр `penalty` устаревшим: elastic-net задаётся через l1_ratio.
SKLEARN_USES_L1_RATIO_ONLY = tuple(int(part) for part in sklearn.__version__.split(".")[:2]) >= (1, 8)
SMOKE_GRID = ((1.0, 0.0),)
log = get_logger("ml.train")


@dataclass(slots=True)
class TrainedModel:
    """Обученная модель и всё, что нужно для экспорта артефакта."""

    scaler: StandardScaler
    classifier: LogisticRegression
    calibrator: LogisticRegression
    weak_centroid: np.ndarray
    mature_centroid: np.ndarray
    threshold: float
    feature_names: tuple[str, ...]
    family: str = "logreg_elasticnet"
    params: dict[str, Any] = field(default_factory=dict)

    def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность слабого сигнала."""
        raw = self.classifier.decision_function(self.scaler.transform(matrix)).reshape(-1, 1)
        return self.calibrator.predict_proba(raw)[:, 1]


@dataclass(slots=True)
class TrainResult:
    """Итог обучения: метрики, baselines, диагностика."""

    model: TrainedModel
    cv_metrics: dict[str, float]
    test_metrics: dict[str, float]
    test_confidence: dict[str, tuple[float, float]]
    baselines: dict[str, dict[str, float]]
    confusion: list[list[int]]
    slices: dict[str, dict[str, dict[str, float]]]
    calibration: dict[str, Any]
    dataset_stats: dict[str, Any]
    static_values: list[dict[str, float]]
    train_index: list[int]
    test_index: list[int]


def compute_static_features(
    rows: Sequence[TrainingRow],
    context: FeatureContext,
    vectors: np.ndarray,
    enrichments: Sequence[dict[str, Any] | None],
) -> list[dict[str, float]]:
    """Признаки, не зависящие от фолда: лексические, коллекционные, энциклопедические.

    Эмбеддинговые близости к центроидам заполняются нулями и пересчитываются в каждом фолде.
    """
    values: list[dict[str, float]] = []
    for index, row in enumerate(rows):
        values.append(
            row_features(row, context, vectors[index], None, None, enrichments[index])
        )
    return values


def _with_centroids(
    static_values: Sequence[dict[str, float]],
    vectors: np.ndarray,
    indexes: Sequence[int],
    weak: np.ndarray,
    mature: np.ndarray,
) -> list[dict[str, float]]:
    """Подставляет `emb_sim_*` для набора строк по переданным центроидам."""
    result: list[dict[str, float]] = []
    for position in indexes:
        vector = vectors[position]
        values = dict(static_values[position])
        values["emb_sim_weak_centroid"] = _cosine(vector, weak)
        values["emb_sim_mature_centroid"] = _cosine(vector, mature)
        result.append(values)
    return result


def _cosine(first: np.ndarray, second: np.ndarray) -> float:
    """Косинус между векторами одинаковой размерности."""
    if second is None or second.size == 0 or first.size != second.size:
        return 0.0
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator == 0.0:
        return 0.0
    return float(np.clip(float(first @ second) / denominator, -1.0, 1.0))


def train_model(
    rows: Sequence[TrainingRow],
    context: FeatureContext,
    vectors: np.ndarray,
    enrichments: Sequence[dict[str, Any] | None],
    seed: int,
    holdout_share: float,
    cv_repeats: int,
    cv_folds: int,
    min_precision: float,
    smoke: bool = False,
) -> TrainResult:
    """Полный цикл обучения и честной оценки."""
    labels = np.asarray([row.label for row in rows])
    static_values = compute_static_features(rows, context, vectors, enrichments)
    indexes = np.arange(len(rows))
    train_index, test_index = train_test_split(
        indexes, test_size=holdout_share, stratify=labels, random_state=seed
    )
    log.info(
        "ml.train.split",
        train=len(train_index),
        test=len(test_index),
        positives_train=int(labels[train_index].sum()),
    )

    grid = SMOKE_GRID if smoke else LR_GRID
    splitter = (
        StratifiedKFold(n_splits=max(2, min(cv_folds, 3)), shuffle=True, random_state=seed)
        if smoke
        else RepeatedStratifiedKFold(n_splits=cv_folds, n_repeats=cv_repeats, random_state=seed)
    )
    folds = list(splitter.split(train_index, labels[train_index]))

    best_params, cv_metrics, oof_scores, oof_labels = _select_model(
        grid, folds, train_index, labels, static_values, vectors, context, seed
    )
    calibrator = _fit_calibrator(oof_scores, oof_labels)
    calibrated = calibrator.predict_proba(oof_scores.reshape(-1, 1))[:, 1]
    threshold = _select_threshold(calibrated, oof_labels, min_precision)
    calibration = {
        "ece": _expected_calibration_error(calibrated, oof_labels),
        "bins": _reliability_bins(calibrated, oof_labels),
        "threshold_rule": f"max F1 при precision ≥ {min_precision:.2f} на CV-предсказаниях",
    }

    weak, mature = centroids_from(vectors[train_index], labels[train_index])
    train_rows = _with_centroids(static_values, vectors, train_index, weak, mature)
    matrix_train = build_matrix(train_rows, context.registry)
    scaler = StandardScaler().fit(matrix_train)
    classifier = _make_classifier(best_params, seed).fit(scaler.transform(matrix_train), labels[train_index])
    model = TrainedModel(
        scaler=scaler,
        classifier=classifier,
        calibrator=calibrator,
        weak_centroid=weak,
        mature_centroid=mature,
        threshold=threshold,
        feature_names=context.registry.model_names,
        params={"C": best_params[0], "l1_ratio": best_params[1]},
    )

    test_rows = _with_centroids(static_values, vectors, test_index, weak, mature)
    matrix_test = build_matrix(test_rows, context.registry)
    probabilities = model.predict_proba(matrix_test)
    predictions = (probabilities >= threshold).astype(int)
    test_metrics = _metrics(labels[test_index], predictions, probabilities)
    confidence = _bootstrap_confidence(labels[test_index], probabilities, threshold, seed)
    baselines = _baselines(
        rows, folds, train_index, labels, static_values, vectors, context, seed, threshold, smoke
    )
    slices = _slices(rows, test_index, labels[test_index], predictions)
    return TrainResult(
        model=model,
        cv_metrics=cv_metrics,
        test_metrics=test_metrics,
        test_confidence=confidence,
        baselines=baselines,
        confusion=confusion_matrix(labels[test_index], predictions).tolist(),
        slices=slices,
        calibration=calibration,
        dataset_stats={
            "rows": len(rows),
            "positives": int(labels.sum()),
            "negatives": int(len(labels) - labels.sum()),
            "balance": float(labels.mean()),
        },
        static_values=static_values,
        train_index=[int(value) for value in train_index],
        test_index=[int(value) for value in test_index],
    )


def _make_classifier(params: tuple[float, float], seed: int) -> LogisticRegression:
    """LR elastic-net (`saga`) с балансировкой классов — модель-кандидат по умолчанию (§12.8 ТЗ)."""
    penalty_c, l1_ratio = params
    common = {
        "solver": "saga",
        "C": penalty_c,
        "l1_ratio": l1_ratio,
        "class_weight": "balanced",
        "max_iter": 5000,
        "random_state": seed,
    }
    if SKLEARN_USES_L1_RATIO_ONLY:
        return LogisticRegression(**common)
    return LogisticRegression(penalty="elasticnet", **common)


def _select_model(
    grid: Sequence[tuple[float, float]],
    folds: Sequence[tuple[np.ndarray, np.ndarray]],
    train_index: np.ndarray,
    labels: np.ndarray,
    static_values: Sequence[dict[str, float]],
    vectors: np.ndarray,
    context: FeatureContext,
    seed: int,
) -> tuple[tuple[float, float], dict[str, float], np.ndarray, np.ndarray]:
    """Перебор гиперпараметров по среднему F1 на CV; возвращает лучшую конфигурацию и OOF-оценки."""
    best: tuple[float, float] | None = None
    best_f1 = -1.0
    best_state: tuple[np.ndarray, np.ndarray, list[float], list[float]] | None = None
    for params in grid:
        scores: list[float] = []
        accuracies: list[float] = []
        oof = np.zeros(len(train_index), dtype=np.float64)
        for fold, (inner_train, inner_valid) in enumerate(folds):
            rows_train = train_index[inner_train]
            rows_valid = train_index[inner_valid]
            weak, mature = centroids_from(vectors[rows_train], labels[rows_train])
            matrix_train = build_matrix(
                _with_centroids(static_values, vectors, rows_train, weak, mature), context.registry
            )
            matrix_valid = build_matrix(
                _with_centroids(static_values, vectors, rows_valid, weak, mature), context.registry
            )
            scaler = StandardScaler().fit(matrix_train)
            model = _make_classifier(params, seed).fit(
                scaler.transform(matrix_train), labels[rows_train]
            )
            decision = model.decision_function(scaler.transform(matrix_valid))
            oof[inner_valid] = decision
            predicted = (decision >= 0).astype(int)
            fold_f1 = float(f1_score(labels[rows_valid], predicted, zero_division=0))
            scores.append(fold_f1)
            accuracies.append(float(accuracy_score(labels[rows_valid], predicted)))
            log.debug("ml.train.fold", model="logreg_elasticnet", fold=fold, f1=round(fold_f1, 4))
        mean_f1 = float(np.mean(scores))
        if mean_f1 > best_f1:
            best_f1 = mean_f1
            best = params
            best_state = (oof, labels[train_index], scores, accuracies)
    assert best is not None and best_state is not None  # noqa: S101 - сетка непуста по построению
    oof, oof_labels, scores, accuracies = best_state
    metrics = {
        "f1_mean": float(np.mean(scores)),
        "f1_std": float(np.std(scores)),
        "accuracy_mean": float(np.mean(accuracies)),
        "accuracy_std": float(np.std(accuracies)),
    }
    log.info("ml.train.result", stage="cv", best_c=best[0], best_l1_ratio=best[1], **metrics)
    return best, metrics, oof, oof_labels


def _fit_calibrator(scores: np.ndarray, labels: np.ndarray) -> LogisticRegression:
    """Калибровка Платта: логистическая регрессия на CV-оценках решающей функции."""
    return LogisticRegression(max_iter=1000).fit(scores.reshape(-1, 1), labels)


def _select_threshold(probabilities: np.ndarray, labels: np.ndarray, min_precision: float) -> float:
    """Порог с максимальным F1 при ограничении на точность; иначе — максимум F1."""
    candidates = sorted({round(float(value), 4) for value in probabilities} | {0.5})
    best_threshold, best_f1 = 0.5, -1.0
    fallback_threshold, fallback_f1 = 0.5, -1.0
    for threshold in candidates:
        if not 0.0 < threshold < 1.0:
            continue
        predicted = (probabilities >= threshold).astype(int)
        f1 = float(f1_score(labels, predicted, zero_division=0))
        precision = float(precision_score(labels, predicted, zero_division=0))
        if f1 > fallback_f1:
            fallback_threshold, fallback_f1 = threshold, f1
        if precision >= min_precision and f1 > best_f1:
            best_threshold, best_f1 = threshold, f1
    if best_f1 < 0:
        log.warning("ml.train.threshold", reason="precision_constraint_unreachable", used=fallback_threshold)
        return fallback_threshold
    return best_threshold


def _metrics(labels: np.ndarray, predictions: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    """Основные метрики качества."""
    return {
        "accuracy": float(accuracy_score(labels, predictions)),
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "f1": float(f1_score(labels, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(labels, probabilities)) if len(set(labels.tolist())) > 1 else 0.0,
        "pr_auc": float(average_precision_score(labels, probabilities))
        if len(set(labels.tolist())) > 1
        else 0.0,
        "n": int(len(labels)),
    }


def _bootstrap_confidence(
    labels: np.ndarray, probabilities: np.ndarray, threshold: float, seed: int
) -> dict[str, tuple[float, float]]:
    """95 % доверительные интервалы метрик теста бутстрэпом (1000 повторов)."""
    generator = np.random.default_rng(seed)
    collected: dict[str, list[float]] = {"accuracy": [], "precision": [], "recall": [], "f1": []}
    size = len(labels)
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = generator.integers(0, size, size)
        if len(set(labels[sample].tolist())) < 2:
            continue
        predicted = (probabilities[sample] >= threshold).astype(int)
        collected["accuracy"].append(float(accuracy_score(labels[sample], predicted)))
        collected["precision"].append(float(precision_score(labels[sample], predicted, zero_division=0)))
        collected["recall"].append(float(recall_score(labels[sample], predicted, zero_division=0)))
        collected["f1"].append(float(f1_score(labels[sample], predicted, zero_division=0)))
    return {
        name: (float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5)))
        for name, values in collected.items()
        if values
    }


def _expected_calibration_error(probabilities: np.ndarray, labels: np.ndarray) -> float:
    """ECE по 10 равным интервалам вероятности."""
    total = len(labels)
    error = 0.0
    for index in range(CALIBRATION_BINS):
        low = index / CALIBRATION_BINS
        high = (index + 1) / CALIBRATION_BINS
        mask = (probabilities > low) & (probabilities <= high) if index else (probabilities <= high)
        if not mask.any():
            continue
        error += mask.sum() / total * abs(float(labels[mask].mean()) - float(probabilities[mask].mean()))
    return float(error)


def _reliability_bins(probabilities: np.ndarray, labels: np.ndarray) -> list[dict[str, float]]:
    """Данные диаграммы надёжности: средняя вероятность и доля позитивов по интервалам."""
    bins: list[dict[str, float]] = []
    for index in range(CALIBRATION_BINS):
        low = index / CALIBRATION_BINS
        high = (index + 1) / CALIBRATION_BINS
        mask = (probabilities > low) & (probabilities <= high) if index else (probabilities <= high)
        if not mask.any():
            continue
        bins.append(
            {
                "bin": f"{low:.1f}–{high:.1f}",
                "count": int(mask.sum()),
                "mean_probability": float(probabilities[mask].mean()),
                "observed_rate": float(labels[mask].mean()),
            }
        )
    return bins


def _baselines(
    rows: Sequence[TrainingRow],
    folds: Sequence[tuple[np.ndarray, np.ndarray]],
    train_index: np.ndarray,
    labels: np.ndarray,
    static_values: Sequence[dict[str, float]],
    vectors: np.ndarray,
    context: FeatureContext,
    seed: int,
    threshold: float,
    smoke: bool,
) -> dict[str, dict[str, float]]:
    """Опорные решения B0–B3 на той же CV-схеме (§12.8 ТЗ)."""
    results: dict[str, dict[str, float]] = {}
    fold_subset = folds[: 5 if not smoke else len(folds)]

    # B0: разность лексических признаков ранней стадии и зрелости с порогом 0
    lexical_scores = np.asarray(
        [
            static_values[index]["lex_emergence_score"] - static_values[index]["lex_maturity_score"]
            for index in train_index
        ]
    )
    results["B0_rules_lexicon"] = _score_binary(labels[train_index], (lexical_scores > 0).astype(int))

    texts = [rows[index].text for index in train_index]
    results["B1_tfidf_lr"] = _cv_text_baseline(texts, labels[train_index], fold_subset, seed)
    results["B2_embeddings_lr"] = _cv_matrix_baseline(
        vectors[train_index], labels[train_index], fold_subset, seed
    )
    try:
        import lightgbm  # noqa: PLC0415 - необязательная зависимость
    except ImportError:
        results["B3_lightgbm"] = {"skipped": 1.0}
    else:
        results["B3_lightgbm"] = _cv_lightgbm(
            lightgbm, folds[:5], train_index, labels, static_values, vectors, context, seed
        )
    return results


def _score_binary(labels: np.ndarray, predictions: np.ndarray) -> dict[str, float]:
    """Метрики бинарного решения без вероятностей."""
    return {
        "accuracy": float(accuracy_score(labels, predictions)),
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "f1": float(f1_score(labels, predictions, zero_division=0)),
    }


def _cv_text_baseline(
    texts: Sequence[str], labels: np.ndarray, folds: Sequence[tuple[np.ndarray, np.ndarray]], seed: int
) -> dict[str, float]:
    """B1: TF-IDF (1–2 граммы) + логистическая регрессия."""
    scores: list[float] = []
    accuracies: list[float] = []
    for inner_train, inner_valid in folds:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=20000)
        matrix = vectorizer.fit_transform([texts[index] for index in inner_train])
        model = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed)
        model.fit(matrix, labels[inner_train])
        predicted = model.predict(vectorizer.transform([texts[index] for index in inner_valid]))
        scores.append(float(f1_score(labels[inner_valid], predicted, zero_division=0)))
        accuracies.append(float(accuracy_score(labels[inner_valid], predicted)))
    return {"f1_mean": float(np.mean(scores)), "accuracy_mean": float(np.mean(accuracies))}


def _cv_matrix_baseline(
    matrix: np.ndarray, labels: np.ndarray, folds: Sequence[tuple[np.ndarray, np.ndarray]], seed: int
) -> dict[str, float]:
    """B2: эмбеддинг текста + логистическая регрессия."""
    scores: list[float] = []
    accuracies: list[float] = []
    for inner_train, inner_valid in folds:
        model = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=seed)
        model.fit(matrix[inner_train], labels[inner_train])
        predicted = model.predict(matrix[inner_valid])
        scores.append(float(f1_score(labels[inner_valid], predicted, zero_division=0)))
        accuracies.append(float(accuracy_score(labels[inner_valid], predicted)))
    return {"f1_mean": float(np.mean(scores)), "accuracy_mean": float(np.mean(accuracies))}


def _cv_lightgbm(
    lightgbm: Any,
    folds: Sequence[tuple[np.ndarray, np.ndarray]],
    train_index: np.ndarray,
    labels: np.ndarray,
    static_values: Sequence[dict[str, float]],
    vectors: np.ndarray,
    context: FeatureContext,
    seed: int,
) -> dict[str, float]:
    """B3: LightGBM на 24 признаках реестра (если пакет установлен)."""
    scores: list[float] = []
    accuracies: list[float] = []
    for inner_train, inner_valid in folds:
        rows_train = train_index[inner_train]
        rows_valid = train_index[inner_valid]
        weak, mature = centroids_from(vectors[rows_train], labels[rows_train])
        matrix_train = build_matrix(
            _with_centroids(static_values, vectors, rows_train, weak, mature), context.registry
        )
        matrix_valid = build_matrix(
            _with_centroids(static_values, vectors, rows_valid, weak, mature), context.registry
        )
        model = lightgbm.LGBMClassifier(
            num_leaves=15, n_estimators=200, min_child_samples=5, random_state=seed, verbose=-1
        )
        model.fit(matrix_train, labels[rows_train])
        predicted = model.predict(matrix_valid)
        scores.append(float(f1_score(labels[rows_valid], predicted, zero_division=0)))
        accuracies.append(float(accuracy_score(labels[rows_valid], predicted)))
    return {"f1_mean": float(np.mean(scores)), "accuracy_mean": float(np.mean(accuracies))}


def _slices(
    rows: Sequence[TrainingRow], test_index: Sequence[int], labels: np.ndarray, predictions: np.ndarray
) -> dict[str, dict[str, dict[str, float]]]:
    """Метрики в разрезе подтипа негатива и области (§12.8 ТЗ)."""
    result: dict[str, dict[str, dict[str, float]]] = {"label_kind": {}, "domain_tag": {}}
    for key, getter in (("label_kind", lambda row: row.label_kind), ("domain_tag", lambda row: row.domain_tag)):
        groups: dict[str, list[int]] = {}
        for position, index in enumerate(test_index):
            groups.setdefault(getter(rows[index]), []).append(position)
        for name, positions in sorted(groups.items()):
            subset = np.asarray(positions)
            result[key][name] = {
                "n": int(len(subset)),
                "accuracy": float(accuracy_score(labels[subset], predictions[subset])),
            }
    return result


def sigmoid(value: float) -> float:
    """Логистическая функция (используется в отчётах)."""
    return 1.0 / (1.0 + math.exp(-value))
