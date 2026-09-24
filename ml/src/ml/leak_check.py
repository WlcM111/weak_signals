"""Сценарий `leak-check`: контроль утечки маркеров разметки в признаки (§6.1 HANDOFF).

Строится нарочно «плохая» модель: TF-IDF только по маркерным токенам разметки. Если она отделяет
классы лучше случайного, значит описания позитивов всё ещё содержат язык колонок методологов,
и список `leak_patterns.yaml` неполон.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import f1_score

from ml.dataset import TrainingRow

MAX_ACCEPTABLE_F1 = 0.6


@dataclass(frozen=True, slots=True)
class LeakCheckResult:
    """Итог проверки утечки."""

    marker_f1: float
    threshold: float
    passed: bool
    markers_found: int

    @property
    def summary(self) -> str:
        """Однострочный вывод для отчёта."""
        verdict = "пройдена" if self.passed else "ПРОВАЛЕНА"
        return (
            f"проверка утечки {verdict}: F1 модели на маркерных токенах {self.marker_f1:.3f} "
            f"при пороге {self.threshold:.2f}, маркеров в текстах {self.markers_found}"
        )


def length_only_accuracy(rows: Sequence[TrainingRow], seed: int) -> float:
    """Точность модели с единственным признаком — логарифмом длины текста (кросс-валидация 5 фолдов).

    Контроль ложного признака: значение заметно выше доли большего класса означает, что классы
    различимы по длине текста, и метрики основной модели завышены стилем авторов.
    """
    labels = np.asarray([row.label for row in rows])
    if len(set(labels.tolist())) < 2:
        return 0.0
    lengths = np.log(np.asarray([max(len(row.text), 1) for row in rows], dtype=np.float64)).reshape(-1, 1)
    predictions = cross_val_predict(
        LogisticRegression(max_iter=1000),
        lengths,
        labels,
        cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=seed),
    )
    return float((predictions == labels).mean())


def run_leak_check(
    rows: Sequence[TrainingRow],
    marker_tokens: Sequence[str],
    seed: int,
    threshold: float = MAX_ACCEPTABLE_F1,
) -> LeakCheckResult:
    """Обучает контрольную модель только на маркерных токенах и сравнивает F1 с порогом."""
    texts = [row.text.lower() for row in rows]
    labels = np.asarray([row.label for row in rows])
    vocabulary = sorted({token.lower() for token in marker_tokens})
    found = sum(1 for text in texts if any(token in text for token in vocabulary))
    vectorizer = TfidfVectorizer(vocabulary=vocabulary, lowercase=True)
    matrix = vectorizer.fit_transform(texts)
    if matrix.nnz == 0 or len(set(labels.tolist())) < 2:
        return LeakCheckResult(marker_f1=0.0, threshold=threshold, passed=True, markers_found=found)
    predictions = cross_val_predict(
        LogisticRegression(max_iter=1000, class_weight="balanced"),
        matrix,
        labels,
        cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=seed),
    )
    score = float(f1_score(labels, predictions, zero_division=0))
    return LeakCheckResult(
        marker_f1=score, threshold=threshold, passed=score <= threshold, markers_found=found
    )
