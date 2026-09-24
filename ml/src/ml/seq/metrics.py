"""Метрики с явными знаменателями, калибровка Платта, порог и бутстрэп по независимым группам."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

from ml.seq import linear


def roc_auc(y: np.ndarray, s: np.ndarray) -> float | None:
    """ROC-AUC; None, если в выборке один класс (неприменимо)."""
    y = np.asarray(y)
    return float(roc_auc_score(y, s)) if 0 < y.sum() < len(y) else None


def pr_auc(y: np.ndarray, s: np.ndarray) -> float | None:
    """Average precision; None без позитивов."""
    y = np.asarray(y)
    return float(average_precision_score(y, s)) if y.sum() > 0 else None


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def ece(y: np.ndarray, p: np.ndarray, bins: int = 5) -> float:
    """ECE по равным интервалам [0,1] (число интервалов в отчёте)."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    total, error = len(y), 0.0
    edges = np.linspace(0, 1, bins + 1)
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= low) & (p < high) if high < 1 else (p >= low) & (p <= high)
        if mask.any():
            error += mask.sum() / total * abs(y[mask].mean() - p[mask].mean())
    return float(error)


def precision_at_k(y: np.ndarray, s: np.ndarray, topics: Sequence[str], k: int) -> tuple[float | None, int]:
    """Средняя по темам доля релевантных в top-k; учитываются темы с ≥ k кандидатами."""
    values = []
    for topic in sorted(set(topics)):
        idx = [i for i, t in enumerate(topics) if t == topic]
        if len(idx) < k:
            continue
        order = sorted(idx, key=lambda i: -s[i])[:k]
        values.append(float(np.mean(np.asarray(y)[order])))
    return (float(np.mean(values)) if values else None), len(values)


def ndcg_at_k(y: np.ndarray, s: np.ndarray, topics: Sequence[str], k: int) -> tuple[float | None, int]:
    """nDCG@k с бинарной релевантностью, среднее по темам с хотя бы одним позитивом."""
    values = []
    for topic in sorted(set(topics)):
        idx = [i for i, t in enumerate(topics) if t == topic]
        rel = np.asarray(y)[idx]
        if rel.sum() == 0:
            continue
        order = np.argsort(-np.asarray(s)[idx], kind="stable")[:k]
        dcg = sum(rel[j] / np.log2(r + 2) for r, j in enumerate(order))
        ideal = sum(1.0 / np.log2(r + 2) for r in range(min(k, int(rel.sum()))))
        values.append(dcg / ideal)
    return (float(np.mean(values)) if values else None), len(values)


def at_threshold(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    """Precision/recall/F1 и матрица ошибок при пороге."""
    y, pred = np.asarray(y), (np.asarray(p) >= threshold).astype(int)
    tp, fp = int(((pred == 1) & (y == 1)).sum()), int(((pred == 1) & (y == 0)).sum())
    fn, tn = int(((pred == 0) & (y == 1)).sum()), int(((pred == 0) & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"threshold": float(threshold), "precision": precision, "recall": recall, "f1": f1,
            "accuracy": (tp + tn) / len(y), "tp": tp, "fp": fp, "fn": fn, "tn": tn, "coverage": float(pred.mean())}


def platt(scores: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """Калибровка Платта на внефолдовых оценках."""
    theta, _ = linear.fit([linear.Block(np.asarray(scores).reshape(-1, 1), np.asarray(y, dtype=float),
                                        np.ones(len(y)))], 1e-3)
    return {"slope": float(theta[0]), "offset": float(theta[1])}


def calibrated(scores: np.ndarray, cal: dict[str, float]) -> np.ndarray:
    z = cal["slope"] * np.asarray(scores) + cal["offset"]
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def best_f1_threshold(p: np.ndarray, y: np.ndarray) -> float:
    """Порог максимума F1 на development-предсказаниях."""
    best, best_f1 = 0.5, -1.0
    for threshold in sorted(set(np.round(np.asarray(p), 4).tolist())):
        if not 0.0 < threshold < 1.0:
            continue
        f1 = at_threshold(y, p, threshold)["f1"]
        if f1 > best_f1:
            best, best_f1 = threshold, f1
    return float(best)


def summary(y: np.ndarray, s: np.ndarray, topics: Sequence[str]) -> dict[str, float | int | None]:
    """Ранговые метрики набора."""
    p3, n3 = precision_at_k(y, s, topics, 3)
    p5, n5 = precision_at_k(y, s, topics, 5)
    nd, nn = ndcg_at_k(y, s, topics, 5)
    return {"n": int(len(y)), "positives": int(np.sum(y)), "pr_auc": pr_auc(y, s), "roc_auc": roc_auc(y, s),
            "p_at_3": p3, "p_at_3_topics": n3, "p_at_5": p5, "p_at_5_topics": n5, "ndcg_at_5": nd, "ndcg_topics": nn}


def bootstrap_ci(
    y: np.ndarray, s: np.ndarray, clusters: Sequence[str], fn: Callable[[np.ndarray, np.ndarray], float | None],
    n: int = 1000, seed: int = 20260924,
) -> tuple[float | None, float | None]:
    """95 % ДИ перевыборкой независимых кластеров (темы или группы)."""
    rng = np.random.default_rng(seed)
    names = sorted(set(clusters))
    index = {name: [i for i, c in enumerate(clusters) if c == name] for name in names}
    values = []
    for _ in range(n):
        picked = rng.choice(len(names), len(names), replace=True)
        rows = [i for j in picked for i in index[names[j]]]
        value = fn(np.asarray(y)[rows], np.asarray(s)[rows])
        if value is not None:
            values.append(value)
    if len(values) < n * 0.5:
        return None, None
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def paired_delta(
    y: np.ndarray, s1: np.ndarray, s2: np.ndarray, clusters: Sequence[str],
    fn: Callable[[np.ndarray, np.ndarray], float | None], n: int = 1000, seed: int = 20260924,
) -> dict[str, float | None]:
    """Парная разность метрик (s1 − s2) на одних и тех же перевыборках кластеров."""
    rng = np.random.default_rng(seed)
    names = sorted(set(clusters))
    index = {name: [i for i, c in enumerate(clusters) if c == name] for name in names}
    y, s1, s2 = np.asarray(y), np.asarray(s1), np.asarray(s2)
    base1, base2 = fn(y, s1), fn(y, s2)
    deltas = []
    for _ in range(n):
        picked = rng.choice(len(names), len(names), replace=True)
        rows = [i for j in picked for i in index[names[j]]]
        a, b = fn(y[rows], s1[rows]), fn(y[rows], s2[rows])
        if a is not None and b is not None:
            deltas.append(a - b)
    if base1 is None or base2 is None or len(deltas) < n * 0.5:
        return {"delta": None, "ci_low": None, "ci_high": None, "share_positive": None}
    return {"delta": float(base1 - base2), "ci_low": float(np.percentile(deltas, 2.5)),
            "ci_high": float(np.percentile(deltas, 97.5)), "share_positive": float(np.mean(np.asarray(deltas) > 0))}
