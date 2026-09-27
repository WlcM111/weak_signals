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
from sklearn.model_selection import StratifiedKFold

from ws_common.signal_classifier import features

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


def scores(y: list[int], p: list[int]) -> dict:
    return {"n": len(y), "accuracy": round(accuracy_score(y, p), 4), "precision": round(precision_score(y, p, zero_division=0), 4),
            "recall": round(recall_score(y, p, zero_division=0), 4), "f1": round(f1_score(y, p, zero_division=0), 4)}


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="локальный классификатор слабого сигнала")
    parser.add_argument("--own", required=True)
    parser.add_argument("--organizers", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    own, org = load(args.own), load(args.organizers)
    vec_own, model_own = fit(own)
    report = {
        "own_rows": len(own), "organizer_rows": len(org),
        "own_cv": cross(own, []),
        "own_to_organizers": scores([int(r["label"]) for r in org], predict(vec_own, model_own, org)),
        "organizers_cv": cross(org, own),
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
    print(json.dumps({k: report[k] for k in ("own_rows", "organizer_rows", "own_cv", "own_to_organizers", "organizers_cv")},
                     ensure_ascii=False, indent=1))
    print("признаки слабого сигнала:", [k for k, _ in report["top_weak_signal_features"][:10]])
    print("признаки зрелости:", [k for k, _ in report["top_mature_features"][:10]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
