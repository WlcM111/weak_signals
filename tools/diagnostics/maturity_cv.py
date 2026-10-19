"""Опыт R-013 / R-022 (часть b): отличает ли локальный классификатор по эмбеддингам e5 карточки «зрелое» от слабых
сигналов (рыночных и ранних исследований) на ручной разметке нескольких прогонов. Запуск ВНУТРИ контейнера analyzer:
тот же эмбеддер, что у analyzer (intfloat/multilingual-e5-base из кэша /hf-cache), сеть не нужна.

Проверка честная: обучение на всех прогонах, кроме одного, оценка на оставшемся (leave-one-run-out). Карточки
проверочного прогона, почти совпадающие с обучающими (косинус ≥ 0,95), из оценки исключаются: одни и те же сигналы
повторяются между прогонами. Порог выбирается только на обучающих прогонах так, чтобы терять не больше 5 % слабых.

Правило внедрения (заранее): в среднем по прогонам убрано не меньше 50 % зрелых при потере не больше 5 % слабых.

Использование: python maturity_cv.py --run имя:карточки:вердикты.csv [--run ...] [--out отчёт.json] (карточки —
JSONL замера e2e или JSON выгрузки заданий с полем items).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression

from analyzer.adapters.outbound.e5_embedder import E5Embedder
from analyzer.config import AnalyzerSettings

WEAK, MATURE = {"слабый сигнал", "ранний"}, "зрелое"
MAX_WEAK_LOSS, MIN_MATURE_CUT, NEAR_DUP = 0.05, 0.5, 0.95


def load_cards(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    data = json.load(open(path, encoding="utf-8"))
    return [{"topic": topic["topic"], **item} for topic in data for item in topic.get("items", [])]


def text_of(card: dict) -> str:
    sources = "; ".join((s.get("title") or "")[:120] for s in (card.get("sources") or [])[:3])
    return f"{card.get('title_ru', '')}. {(card.get('description_ru') or '')[:600]} Источники: {sources}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--out", default="/tmp/maturity_cv.json", help="файл отчёта JSON")
    args = parser.parse_args()
    settings = AnalyzerSettings()
    embedder = E5Embedder(settings.embedding_model, cache_dir=str(settings.hf_home))
    runs = []
    for spec in args.run:
        name, cards_path, verdicts_path = spec.split(":", 2)
        verdicts = {(r["topic"].strip(), r["title_ru"].strip()): r["verdict"].strip()
                    for r in csv.DictReader(open(verdicts_path, encoding="utf-8-sig"))}
        rows = []
        for card in load_cards(cards_path):
            verdict = verdicts.get((card["topic"].strip(), card.get("title_ru", "").strip()))
            if verdict in WEAK or verdict == MATURE:
                rows.append((text_of(card), 1 if verdict == MATURE else 0, verdict, card.get("title_ru", "")[:70]))
        vectors = embedder.encode([row[0] for row in rows], "passage: ")
        runs.append((name, rows, np.asarray(vectors)))
        print(f"{name}: зрелых {sum(r[1] for r in rows)}, слабых {sum(1 - r[1] for r in rows)}")
    report, cuts, losses = [], [], []
    for index, (name, rows, vectors) in enumerate(runs):
        train_x = np.vstack([v for i, (_, _, v) in enumerate(runs) if i != index])
        train_y = np.concatenate([[r[1] for r in rs] for i, (_, rs, _) in enumerate(runs) if i != index])
        model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000).fit(train_x, train_y)
        train_scores = model.predict_proba(train_x)[:, 1]
        threshold = float(np.quantile(train_scores[train_y == 0], 1 - MAX_WEAK_LOSS))
        similar = (vectors @ train_x.T).max(axis=1) >= NEAR_DUP
        keep = ~similar
        scores = model.predict_proba(vectors)[:, 1]
        y = np.array([r[1] for r in rows])
        flagged = scores > threshold
        mature_total, weak_total = int(((y == 1) & keep).sum()), int(((y == 0) & keep).sum())
        cut = float((flagged & (y == 1) & keep).sum()) / mature_total if mature_total else 0.0
        loss = float((flagged & (y == 0) & keep).sum()) / weak_total if weak_total else 0.0
        cuts.append(cut)
        losses.append(loss)
        lost = [rows[i][3] for i in range(len(rows)) if flagged[i] and y[i] == 0 and keep[i]]
        report.append({"run": name, "threshold": round(threshold, 4), "near_duplicates_excluded": int(similar.sum()),
                       "mature": mature_total, "mature_removed": round(cut, 3), "weak": weak_total,
                       "weak_lost": round(loss, 3), "weak_lost_titles": lost})
        print(f"проверка на {name}: зрелых убрано {cut:.0%} из {mature_total}, слабых потеряно {loss:.0%} из {weak_total}"
              f" (исключено почти совпадающих: {int(similar.sum())}); потерянные слабые: {lost[:5]}")
    mean_cut, mean_loss = float(np.mean(cuts)), float(np.mean(losses))
    decision = "ВНЕДРЯТЬ" if mean_cut >= MIN_MATURE_CUT and mean_loss <= MAX_WEAK_LOSS else "НЕ ВНЕДРЯТЬ"
    print(f"\nИТОГ: в среднем зрелых убрано {mean_cut:.0%}, слабых потеряно {mean_loss:.0%} → {decision}")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"folds": report, "mean_mature_removed": mean_cut, "mean_weak_lost": mean_loss, "decision": decision},
                  handle, ensure_ascii=False, indent=1)
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
