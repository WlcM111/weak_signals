"""Обучение и проверка локального классификатора слабого сигнала (ws_common.signal_classifier).

Данные: собственный датасет (ml/data/own/signals_v1.jsonl: конкретные технологии ранней стадии против зрелых,
стандартов, общих понятий, хайпа и обзоров) и датасет организаторов (100 слабых сигналов методологов и 125
негативов). Проверка по ТЗ — точность, Precision, Recall, F1 на данных организаторов:
- own_cv — перекрёстная проверка на собственном датасете;
- own_to_organizers — модель только на собственном датасете, проверка на всех 225 строках организаторов;
- organizers_cv — 5 фолдов по строкам организаторов: обучение на собственном датасете и 4/5 строк
  организаторов, проверка на оставшейся 1/5 (валидационная выборка закрытого датасета).
Итоговая модель обучается на всех строках и сохраняется в JSON вместе с отчётом и важностью признаков.

Запуск: python -m ml.signal_classifier_train --own ml/data/own/signals_v1.jsonl
    --organizers ml/data/labels/dataset_ds-2026.09.19-v2.jsonl --out services/orchestrator/config/signal_classifier.json
    --report ml/reports/signal_classifier/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import LeaveOneGroupOut, StratifiedKFold

from ws_common.signal_classifier import features
from ws_common.signal_classifier import features as base_features

SEED = 20260927
C = 4.0


def text_of(row: dict) -> str:
    return f"{row['title']}. {row.get('description', '')}"


def load(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def fit(rows: list[dict]) -> tuple[DictVectorizer, LogisticRegression]:
    vectorizer = DictVectorizer()
    X = vectorizer.fit_transform([features(text_of(r)) for r in rows])
    model = LogisticRegression(C=C, max_iter=5000, class_weight="balanced").fit(X, [int(r["label"]) for r in rows])
    return vectorizer, model


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """95 % интервал Уилсона для доли k/n; None при нулевом знаменателе (метрика не определена)."""
    if n <= 0:
        return None
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def scores(y: list[int], p: list[int]) -> dict:
    """Метрики с матрицей ошибок и 95 % интервалами (аудит A1/D2: точечные оценки без CI вводили в заблуждение)."""
    tp = sum(1 for a, b in zip(y, p) if a == 1 and b == 1)
    fp = sum(1 for a, b in zip(y, p) if a == 0 and b == 1)
    fn = sum(1 for a, b in zip(y, p) if a == 1 and b == 0)
    tn = sum(1 for a, b in zip(y, p) if a == 0 and b == 0)
    positives = tp + fn
    return {"n": len(y), "accuracy": round(accuracy_score(y, p), 4) if y else None,
            "precision": round(precision_score(y, p, zero_division=0), 4) if y else None,
            "recall": round(recall_score(y, p, zero_division=0), 4) if positives else None,
            "f1": round(f1_score(y, p, zero_division=0), 4) if y else None,
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn}, "positives": positives,
            "majority_accuracy": round(max(positives, len(y) - positives) / len(y), 4) if y else None,
            "ci95": {"accuracy": wilson(tp + tn, len(y)), "precision": wilson(tp, tp + fp), "recall": wilson(tp, positives)}}


def text_length(row: dict) -> int:
    return len(text_of(row))


def length_auc(rows: list[dict]) -> float | None:
    """AUC одной длины текста (Манн — Уитни): насколько классы различимы без содержания. None без обоих классов."""
    pos = [text_length(r) for r in rows if int(r["label"]) == 1]
    neg = [text_length(r) for r in rows if int(r["label"]) == 0]
    if not pos or not neg:
        return None
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def origin_confound(rows: list[dict]) -> dict:
    """Совпадает ли класс с происхождением или разметчиком (аудит A1): если каждое значение поля встречается только
    с одним классом при обоих классах в данных — метрики на этих данных измеряют источник, а не сигнал."""
    result = {}
    labels = {int(r["label"]) for r in rows}
    for field in ("origin", "annotators"):
        by_value: dict[str, set[int]] = {}
        for r in rows:
            if r.get(field) is None:
                continue
            by_value.setdefault(json.dumps(r[field], ensure_ascii=False, sort_keys=True), set()).add(int(r["label"]))
        result[field] = {"values": len(by_value),
                         "class_equals_field": bool(by_value) and labels == {0, 1}
                         and all(len(v) == 1 for v in by_value.values()) and len(by_value) >= 2}
    return result


def length_baseline_cv(rows: list[dict], seed: int = SEED) -> dict:
    """Baseline «только длина текста» с теми же 5 фолдами, что и модель (аудит A1)."""
    labels = [int(r["label"]) for r in rows]
    y_all, p_all = [], []
    for train, test in StratifiedKFold(n_splits=5, shuffle=True, random_state=seed).split(rows, labels):
        X = np.array([[text_length(rows[i])] for i in train], dtype=float)
        model = LogisticRegression(max_iter=1000, class_weight="balanced").fit(X, [labels[i] for i in train])
        y_all += [labels[i] for i in test]
        p_all += model.predict(np.array([[text_length(rows[i])] for i in test], dtype=float)).tolist()
    return scores(y_all, p_all)


def cross_by_group(rows: list[dict], extra: list[dict], field: str = "domain_tag") -> dict:
    """Перенос на новую тему (аудит A2): каждый фолд — один домен целиком, обучение на остальных доменах и `extra`."""
    groups = [str(r.get(field) or "?") for r in rows]
    labels = [int(r["label"]) for r in rows]
    y_all, p_all, per_group = [], [], {}
    if len(set(groups)) < 2:
        return {"error": f"меньше двух групп по полю {field}"}
    for train, test in LeaveOneGroupOut().split(rows, labels, groups):
        train_rows = extra + [rows[i] for i in train]
        if len({int(r["label"]) for r in train_rows}) < 2:
            continue
        vec, model = fit(train_rows)
        y = [labels[i] for i in test]
        p = predict(vec, model, [rows[i] for i in test])
        per_group[groups[test[0]]] = scores(y, p)
        y_all += y
        p_all += p
    result = scores(y_all, p_all)
    result["per_group"] = per_group
    result["group_field"] = field
    return result


def predict(vectorizer: DictVectorizer, model: LogisticRegression, rows: list[dict]) -> list[int]:
    return model.predict(vectorizer.transform([features(text_of(r)) for r in rows])).tolist()


def cross(rows: list[dict], extra: list[dict], seed: int = SEED) -> dict:
    """5 фолдов по `rows`; к обучению каждого фолда добавляется `extra` целиком."""
    y_all, p_all, folds = [], [], []
    labels = [int(r["label"]) for r in rows]
    for train, test in StratifiedKFold(n_splits=5, shuffle=True, random_state=seed).split(rows, labels):
        vec, model = fit(extra + [rows[i] for i in train])
        y = [labels[i] for i in test]
        p = predict(vec, model, [rows[i] for i in test])
        folds.append(scores(y, p)["accuracy"])
        y_all += y
        p_all += p
    result = scores(y_all, p_all)
    result["fold_accuracy"] = folds
    return result


def binary_features(text: str) -> dict[str, float]:
    """B1 (этап 3): те же основы и пары основ, но значение 1,0 без деления на √n — вес признака не зависит от длины."""
    return {key: 1.0 for key in base_features(text)}


def score_length_spearman(vectorizer: DictVectorizer, model: LogisticRegression, rows: list[dict]) -> float | None:
    """Ранговая корреляция оценки модели с длиной текста: насколько решение модели следует за длиной."""
    from scipy.stats import spearmanr  # noqa: PLC0415

    if len(rows) < 3:
        return None
    decision = model.decision_function(vectorizer.transform([features(text_of(r)) for r in rows]))
    value = spearmanr(decision, [text_length(r) for r in rows]).statistic
    return None if value != value else round(float(value), 4)  # NaN — не определена


def b1_length_free(own: list[dict], organizers: list[dict]) -> dict:
    """Эксперимент B1: нормировка 1/√n против бинарных признаков на тех же разбиениях; модель не экспортируется.

    Правило приёмки из плана: вариант без зависимости от длины принимается, только если превзойдёт baseline
    «только длина» на тесте без конфаунда. Такого теста пока нет (A1-b, этап 4), поэтому итог — замер, не замена.
    """
    module = sys.modules[__name__]
    original = module.features
    labels = [int(r["label"]) for r in organizers]
    result: dict = {}
    try:
        for name, featurize in (("sqrt_current", base_features), ("binary_length_free", binary_features)):
            module.features = featurize
            vectorizer, model = fit(own)
            result[name] = {
                "organizers_cv": cross(organizers, own),
                "organizers_by_domain": {k: v for k, v in cross_by_group(organizers, own).items() if k != "per_group"},
                "own_to_organizers": scores(labels, predict(vectorizer, model, organizers)),
                "score_length_spearman_organizers": score_length_spearman(vectorizer, model, organizers),
            }
    finally:
        module.features = original
    result["length_only_baseline_organizers"] = length_baseline_cv(organizers)
    result["decision"] = ("не принимается: правило плана требует превзойти baseline «только длина» на тесте без "
                          "конфаунда, а такого теста нет до этапа 4 (A1-b); экспортируемая модель не меняется")
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="локальный классификатор слабого сигнала")
    parser.add_argument("--own", required=True)
    parser.add_argument("--organizers", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--b1-report", default="", help="этап 3, B1: отчёт эксперимента без зависимости от длины")
    args = parser.parse_args(argv)
    own, org = load(args.own), load(args.organizers)
    vec_own, model_own = fit(own)
    report = {
        "own_rows": len(own), "organizer_rows": len(org),
        "own_cv": cross(own, []),
        "own_to_organizers": scores([int(r["label"]) for r in org], predict(vec_own, model_own, org)),
        "organizers_cv": cross(org, own),
        "organizers_by_domain": cross_by_group(org, own),
        "evaluation_integrity": {
            "organizers_cv_split": "rows (StratifiedKFold): темы и авторы общие для обучения и проверки",
            "organizers_confound": origin_confound(org), "own_confound": origin_confound(own),
            "length_auc": {"organizers": length_auc(org), "own": length_auc(own)},
            "length_only_baseline_cv": {"organizers": length_baseline_cv(org), "own": length_baseline_cv(own)},
        },
    }
    vec, model = fit(own + org)
    names = vec.get_feature_names_out()
    weights = {str(name): round(float(w), 6) for name, w in zip(names, model.coef_[0]) if abs(w) > 1e-6}
    top = sorted(weights.items(), key=lambda item: -item[1])
    report["top_weak_signal_features"] = top[:15]
    report["top_mature_features"] = top[-15:][::-1]
    exported = {"model": "logreg_stem_bigrams_v1", "stem": 6, "C": C, "intercept": round(float(model.intercept_[0]), 6),
                "weights": weights, "trained_on": {"own": Path(args.own).name, "organizers": Path(args.organizers).name,
                                                   "rows": len(own) + len(org)},
                "validation": {k: report[k] for k in ("own_cv", "own_to_organizers", "organizers_cv")}}
    for path, payload in ((args.out, exported), (args.report, report)):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("own_rows", "organizer_rows", "own_cv", "own_to_organizers", "organizers_cv",
                                             "organizers_by_domain", "evaluation_integrity")}, ensure_ascii=False, indent=1))
    if args.b1_report:
        b1 = b1_length_free(own, org)
        Path(args.b1_report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.b1_report).write_text(json.dumps(b1, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("B1:", json.dumps({k: {m: v.get(m) for m in ("organizers_cv", "own_to_organizers", "score_length_spearman_organizers")}
                                 for k, v in b1.items() if isinstance(v, dict) and "organizers_cv" in v}, ensure_ascii=False))
    print("признаки слабого сигнала:", [k for k, _ in report["top_weak_signal_features"][:10]])
    print("признаки зрелости:", [k for k, _ in report["top_mature_features"][:10]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
