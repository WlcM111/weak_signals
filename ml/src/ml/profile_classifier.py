"""Классификатор по профилям живого конвейера: решающая модель, только если она обгоняет рубрику LLM.

Данные: датасет B v2 (карточки живой выдачи с метками) и профили тех же карточек, которые готовит рубрика
judge_v4 (insight.eval_rubric --dataset b: колонки technology_ru и profile_ru). Модель та же, что в
ws_common.signal_classifier (основы слов без годов и служебных слов). Проверка — перекрёстная по темам (5 групп),
сравнение с рубрикой (R > U > отказ, затем уверенность LLM) на тех же строках. Итог пишется в JSON всегда;
поле decision.use_as_scorer = true только если PR-AUC модели выше PR-AUC рубрики.

Запуск: python -m ml.profile_classifier --data-dir ml/data/dataset_b/v2
    --predictions ml/reports/rubric/rubric_b_profiles_predictions.csv --out ml/reports/signal_classifier/profile_classifier.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold

from ml.signal_classifier_train import fit
from ws_common.signal_classifier import features


def load_rows(data_dir: Path, predictions: Path) -> list[dict]:
    verdicts = {row["id"]: row for row in csv.DictReader(predictions.open(encoding="utf-8")) if row.get("pred_code")}
    rows = []
    for name in ("dataset_b_v2.jsonl", "dataset_b_v2_uncertain.jsonl"):
        for line in (data_dir / name).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            verdict = verdicts.get(row["group_id"])
            if row.get("label") is None or verdict is None or not verdict.get("profile_ru"):
                continue
            rubric = {"R": 2.0, "U": 1.0}.get(verdict["pred_code"], 0.0) + 0.1 * float(verdict.get("confidence") or 0.0)
            rows.append({"title": verdict.get("technology_ru") or row.get("title", ""), "description": verdict["profile_ru"],
                         "label": int(row["label"]), "topic": row["topic"], "rubric": rubric})
    return rows


def top_k_precision(y: np.ndarray, score: np.ndarray, topics: list[str], k: int = 3) -> float:
    hits = shown = 0
    for topic in set(topics):
        index = np.array([i for i, t in enumerate(topics) if t == topic])
        top = index[np.argsort(-score[index])][:k]
        hits += int(y[top].sum())
        shown += len(top)
    return round(hits / shown, 4) if shown else 0.0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="классификатор по профилям живого конвейера")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = load_rows(Path(args.data_dir), Path(args.predictions))
    y = np.array([r["label"] for r in rows])
    topics = [r["topic"] for r in rows]
    oof = np.zeros(len(rows))
    for train, test in GroupKFold(n_splits=5).split(rows, y, topics):
        vec, model = fit([rows[i] for i in train])
        X = vec.transform([features(f"{rows[i]['title']}. {rows[i]['description']}") for i in test])
        oof[test] = model.predict_proba(X)[:, 1]
    rubric = np.array([r["rubric"] for r in rows])
    local = {"pr_auc": round(float(average_precision_score(y, oof)), 4), "roc_auc": round(float(roc_auc_score(y, oof)), 4),
             "precision_top3_per_topic": top_k_precision(y, oof, topics)}
    base = {"pr_auc": round(float(average_precision_score(y, rubric)), 4), "roc_auc": round(float(roc_auc_score(y, rubric)), 4),
            "precision_top3_per_topic": top_k_precision(y, rubric, topics)}
    use = local["pr_auc"] > base["pr_auc"]
    vec, model = fit(rows)
    weights = {str(n): round(float(w), 6) for n, w in zip(vec.get_feature_names_out(), model.coef_[0]) if abs(w) > 1e-6}
    out = {"model": "profile_logreg_v1", "intercept": round(float(model.intercept_[0]), 6), "weights": weights,
           "validation": {"rows": len(rows), "positives": int(y.sum()), "topics": len(set(topics)),
                          "local_model": local, "rubric_only": base},
           "decision": {"use_as_scorer": use, "rule": "PR-AUC модели по темам выше PR-AUC рубрики"}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"validation": out["validation"], "decision": out["decision"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
