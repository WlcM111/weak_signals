"""Обучение и проверка локальной модели слабого сигнала (уверенность и порядок выдачи в режиме rubric).

Данные: датасет B v2 (карточки живой выдачи с метками) и вердикты рубрики LLM по тем же карточкам
(ml/reports/rubric/rubric_b_*_predictions.csv). Признаки — ws_common.rubric_model. Проверка — перекрёстная по
темам (5 групп): модель не видит тему, на которой её проверяют. Сравнение с двумя базовыми линиями:
только рубрика LLM (R > U > отказ, затем уверенность) и только скор analyzer. Итоговая модель обучается на всех
размеченных строках и сохраняется в JSON вместе с метриками проверки.

Запуск: python -m ml.rubric_ranker --data-dir ml/data/dataset_b/v2
    --predictions ml/reports/rubric/rubric_b_pro_v2_predictions.csv --out ml/reports/rubric/rubric_ranker.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

from ws_common.rubric_model import FEATURES, features

STAGE_MAP = {1: 2, 2: 3, 3: 4, 4: 4}
TREND_MAP = {1: 2, 2: 3, 3: 3}


def dataset(data_dir: Path, predictions: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict]]:
    """Матрица признаков, метки, темы и исходные строки размеченных карточек."""
    verdicts = {row["id"]: row for row in csv.DictReader(predictions.open(encoding="utf-8")) if row.get("pred_code")}
    rows = [json.loads(line) for name in ("dataset_b_v2.jsonl", "dataset_b_v2_uncertain.jsonl")
            for line in (data_dir / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    X, y, topics, kept = [], [], [], []
    for row in rows:
        verdict = verdicts.get(row["group_id"])
        if row.get("label") is None or verdict is None:
            continue
        refs = row.get("evidence_refs") or []
        years = [int(ref["published_at"][:4]) if (ref.get("published_at") or "")[:4].isdigit() else None for ref in refs]
        values = features(verdict["pred_code"], float(verdict["confidence"] or 0.0),
                          STAGE_MAP.get(int(verdict["stage_pred"]), 2), TREND_MAP.get(int(verdict["trend_pred"]), 2),
                          [ref.get("source_type", "") for ref in refs], [ref.get("trust_level", "") for ref in refs],
                          [ref.get("title", "") for ref in refs], years,
                          (row.get("observed_runtime") or {}).get("score"))
        X.append([values[name] for name in FEATURES])
        y.append(int(row["label"]))
        topics.append(row["topic"])
        kept.append(row)
    return np.asarray(X, dtype=float), np.asarray(y), np.asarray(topics), kept


def rubric_only(X: np.ndarray) -> np.ndarray:
    names = list(FEATURES)
    return X[:, names.index("code_R")] * 2 + X[:, names.index("code_U")] + X[:, names.index("llm_confidence")] * 0.1


def metrics(y: np.ndarray, score: np.ndarray, topics: np.ndarray, k: int = 3) -> dict:
    """ROC-AUC, PR-AUC и доля позитивов в первых k карточках каждой темы (как ТОП выдачи)."""
    hits, shown = 0, 0
    for topic in set(topics.tolist()):
        index = np.where(topics == topic)[0]
        top = index[np.argsort(-score[index])][:k]
        hits += int(y[top].sum())
        shown += len(top)
    return {"roc_auc": round(float(roc_auc_score(y, score)), 4), "pr_auc": round(float(average_precision_score(y, score)), 4),
            f"precision_top{k}_per_topic": round(hits / shown, 4) if shown else None}


def train(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, LogisticRegression]:
    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale == 0] = 1.0
    model = LogisticRegression(C=0.5, max_iter=2000).fit((X - mean) / scale, y)
    return mean, scale, model


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="локальная модель слабого сигнала для режима rubric")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    X, y, topics, _ = dataset(Path(args.data_dir), Path(args.predictions))
    oof = np.zeros(len(y))
    for train_index, test_index in GroupKFold(n_splits=5).split(X, y, topics):
        mean, scale, model = train(X[train_index], y[train_index])
        oof[test_index] = model.predict_proba((X[test_index] - mean) / scale)[:, 1]
    report = {"rows": int(len(y)), "positives": int(y.sum()), "topics": int(len(set(topics.tolist()))),
              "cv_by_topic": {"local_model": {**metrics(y, oof, topics), "brier": round(float(brier_score_loss(y, oof)), 4)},
                              "rubric_only": metrics(y, rubric_only(X), topics),
                              "analyzer_only": metrics(y, X[:, list(FEATURES).index("analyzer_score")], topics)}}
    mean, scale, model = train(X, y)
    exported = {"features": list(FEATURES), "mean": mean.round(6).tolist(), "scale": scale.round(6).tolist(),
                "coef": model.coef_[0].round(6).tolist(), "intercept": round(float(model.intercept_[0]), 6),
                "trained_on": {"data_dir": str(args.data_dir), "predictions": Path(args.predictions).name}, **report}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(exported, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("коэффициенты:", {name: round(c, 3) for name, c in zip(FEATURES, model.coef_[0])})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
