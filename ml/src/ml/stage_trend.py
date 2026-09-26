"""Обучаемая базовая модель стадии (1–4) и тренда (1–3) на 100 строках организаторов — планка для рубрики LLM.

TF-IDF по символьным n-граммам названия и описания + логистическая регрессия, повторная стратифицированная
кросс-валидация 5×5; рядом — «самый частый класс». Если модель не лучше самого частого класса, стадию и тренд
на ста строках не выучить, и их определяет рубрика LLM по шкале организаторов, проверяемая на тех же строках.

Запуск: python -m ml.stage_trend --data ml/data/labels/dataset_ds-2026.09.19-v2.jsonl --out ml/reports/rubric
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, cohen_kappa_score, mean_absolute_error
from sklearn.model_selection import RepeatedStratifiedKFold

SEED = 20260926
# Тренд 1 у организаторов всего в 4 строках из 100 — меньше числа фолдов; sklearn об этом предупреждает, это ожидаемо.
warnings.filterwarnings("ignore", message="The least populated class in y has only")


def evaluate(texts: list[str], labels: list[int], splits: int = 5, repeats: int = 5) -> dict:
    """Кросс-валидация модели и самого частого класса (класс берётся по обучающей части фолда)."""
    y = np.asarray(labels)
    model_acc, model_mae, model_qwk, base_acc, base_mae = [], [], [], [], []
    folds = RepeatedStratifiedKFold(n_splits=splits, n_repeats=repeats, random_state=SEED)
    for train, test in folds.split(texts, y):
        vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, sublinear_tf=True)
        train_x = vectorizer.fit_transform([texts[i] for i in train])
        test_x = vectorizer.transform([texts[i] for i in test])
        model = LogisticRegression(C=3.0, max_iter=2000, class_weight="balanced").fit(train_x, y[train])
        predicted = model.predict(test_x)
        majority = Counter(y[train].tolist()).most_common(1)[0][0]
        model_acc.append(accuracy_score(y[test], predicted))
        model_mae.append(mean_absolute_error(y[test], predicted))
        model_qwk.append(cohen_kappa_score(y[test], predicted, weights="quadratic"))
        base_acc.append(accuracy_score(y[test], np.full(len(test), majority)))
        base_mae.append(mean_absolute_error(y[test], np.full(len(test), majority)))
    return {"n": len(labels), "distribution": dict(sorted(Counter(labels).items())),
            "model": {"accuracy": round(float(np.mean(model_acc)), 4), "accuracy_std": round(float(np.std(model_acc)), 4),
                      "mae": round(float(np.mean(model_mae)), 4), "qwk": round(float(np.mean(model_qwk)), 4)},
            "majority": {"accuracy": round(float(np.mean(base_acc)), 4), "mae": round(float(np.mean(base_mae)), 4)}}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="базовая модель стадии и тренда на строках организаторов")
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in Path(args.data).read_text(encoding="utf-8").splitlines() if line.strip()]
    positives = [row for row in rows if row.get("label") == 1 and row.get("stage_ordinal") and row.get("trend_ordinal")]
    texts = [f"{row['title']}. {row.get('description', '')}" for row in positives]
    report = {"data": args.data, "rows": len(positives),
              "stage": evaluate(texts, [int(row["stage_ordinal"]) for row in positives]),
              "trend": evaluate(texts, [int(row["trend_ordinal"]) for row in positives])}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stage_trend_baseline.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
