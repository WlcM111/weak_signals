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
MARKET = {"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT", "CORPORATE_BLOG", "GOVERNMENT"}


def load_cards(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    data = json.load(open(path, encoding="utf-8"))
    return [{"topic": topic["topic"], **item} for topic in data for item in topic.get("items", [])]


def research_only(card: dict) -> bool:
    types = {s.get("source_type") or "" for s in card.get("sources") or []}
    return bool(types) and not (types & MARKET)


def text_of(card: dict) -> str:
    sources = "; ".join((s.get("title") or "")[:120] for s in (card.get("sources") or [])[:3])
    return f"{card.get('title_ru', '')}. {(card.get('description_ru') or '')[:600]} Источники: {sources}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--out", default="/tmp/maturity_cv.json", help="файл отчёта JSON")
    parser.add_argument("--model", default="", help="модель эмбеддингов (по умолчанию — модель analyzer, e5-base); "
                        "например intfloat/multilingual-e5-large или BAAI/bge-m3")
    parser.add_argument("--prefix", default="passage: ", help="префикс текста (e5 — «passage: », bge-m3 — пусто)")
    parser.add_argument("--scope", choices=("all", "research"), default="all",
                        help="research — классификатор обучается и применяется только к карточкам с научными источниками; "
                             "карточки СМИ не трогаются, но учитываются в доле потерянных слабых сигналов")
    parser.add_argument("--train-loss", type=float, default=MAX_WEAK_LOSS,
                        help="доля слабых, которую порог может отсечь на обучающих прогонах")
    parser.add_argument("--max-loss", type=float, default=MAX_WEAK_LOSS,
                        help="правило внедрения: потеря слабых на КАЖДОМ проверочном прогоне с ≥ 10 зрелыми не больше")
    parser.add_argument("--min-cut", type=float, default=MIN_MATURE_CUT,
                        help="правило внедрения: средняя доля убранных зрелых по таким прогонам не меньше")
    args = parser.parse_args()
    settings = AnalyzerSettings()
    model_name = args.model or settings.embedding_model
    embedder = E5Embedder(model_name, cache_dir=str(settings.hf_home))
    print(f"модель эмбеддингов: {model_name}")
    runs = []
    for spec in args.run:
        name, cards_path, verdicts_path = spec.split(":", 2)
        verdicts = {(r["topic"].strip(), r["title_ru"].strip()): r["verdict"].strip()
                    for r in csv.DictReader(open(verdicts_path, encoding="utf-8-sig"))}
        rows = []
        for card in load_cards(cards_path):
            verdict = verdicts.get((card["topic"].strip(), card.get("title_ru", "").strip()))
            if verdict in WEAK or verdict == MATURE:
                rows.append((text_of(card), 1 if verdict == MATURE else 0, verdict, card.get("title_ru", "")[:70],
                             research_only(card)))
        vectors = embedder.encode([row[0] for row in rows], args.prefix)
        runs.append((name, rows, np.asarray(vectors)))
        print(f"{name}: зрелых {sum(r[1] for r in rows)}, слабых {sum(1 - r[1] for r in rows)}")
    report, cuts, losses, informative = [], [], [], []
    for index, (name, rows, vectors) in enumerate(runs):
        in_scope = lambda rs: np.array([args.scope == "all" or r[4] for r in rs])  # noqa: E731
        train_rows = [r for i, (_, rs, _) in enumerate(runs) if i != index for r in rs]
        train_x = np.vstack([v for i, (_, _, v) in enumerate(runs) if i != index])
        train_y = np.array([r[1] for r in train_rows])
        train_mask = in_scope(train_rows)
        model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000).fit(train_x[train_mask],
                                                                                      train_y[train_mask])
        train_scores = model.predict_proba(train_x)[:, 1]
        weak_train = train_scores[(train_y == 0) & train_mask]
        # порог отсекает не больше train-loss от ВСЕХ слабых обучающих прогонов (слабые СМИ вне охвата не теряются)
        allowed = args.train_loss * int((train_y == 0).sum())
        ranked = np.sort(weak_train)[::-1]
        threshold = float(ranked[int(allowed)]) if int(allowed) < len(ranked) else float(ranked[-1]) - 1e-9
        similar = (vectors @ train_x.T).max(axis=1) >= NEAR_DUP
        keep = ~similar
        scores = model.predict_proba(vectors)[:, 1]
        y = np.array([r[1] for r in rows])
        flagged = (scores > threshold) & in_scope(rows)
        mature_total, weak_total = int(((y == 1) & keep).sum()), int(((y == 0) & keep).sum())
        cut = float((flagged & (y == 1) & keep).sum()) / mature_total if mature_total else 0.0
        loss = float((flagged & (y == 0) & keep).sum()) / weak_total if weak_total else 0.0
        if mature_total >= 10:
            informative.append(name)
            cuts.append(cut)
            losses.append(loss)
        lost = [rows[i][3] for i in range(len(rows)) if flagged[i] and y[i] == 0 and keep[i]]
        report.append({"run": name, "threshold": round(threshold, 4), "near_duplicates_excluded": int(similar.sum()),
                       "mature": mature_total, "mature_removed": round(cut, 3), "weak": weak_total,
                       "weak_lost": round(loss, 3), "weak_lost_titles": lost, "informative": mature_total >= 10})
        print(f"проверка на {name}: зрелых убрано {cut:.0%} из {mature_total}, слабых потеряно {loss:.0%} из {weak_total}"
              f" (исключено почти совпадающих: {int(similar.sum())}){'' if mature_total >= 10 else ' — мало зрелых, в решение не входит'};"
              f" потерянные слабые: {lost}")
    mean_cut = float(np.mean(cuts)) if cuts else 0.0
    mean_loss = float(np.mean(losses)) if losses else 1.0
    worst = max(losses) if losses else 1.0
    decision = "ВНЕДРЯТЬ" if cuts and mean_cut >= args.min_cut and worst <= args.max_loss else "НЕ ВНЕДРЯТЬ"
    print(f"\nИТОГ (прогоны {informative}; охват: {args.scope}; порог обучения: {args.train_loss:.0%}): в среднем зрелых "
          f"убрано {mean_cut:.0%}, слабых потеряно {mean_loss:.0%}, худший прогон {worst:.0%} → {decision} "
          f"(правило: худший ≤ {args.max_loss:.0%}, среднее убранных ≥ {args.min_cut:.0%})")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"model": model_name, "folds": report, "mean_mature_removed": mean_cut, "mean_weak_lost": mean_loss,
                   "decision": decision},
                  handle, ensure_ascii=False, indent=1)
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
