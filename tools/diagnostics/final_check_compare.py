"""Финальная проверка готовых карточек на ручной разметке: сколько зрелых убирает и сколько слабых теряет (этап 5.4).

Использование:
  python3 tools/diagnostics/final_check_compare.py <predictions.csv> [...] [--dev-runs e2e501] [--tau 0.7]
      [--sweep] [--lost потери.csv]

Файлы — `rubric_b_<label>_predictions.csv` из `python -m insight.eval_rubric --dataset b --final-check`. Карточка
убрана, если код вердикта входит в FINAL_DROP (как в orchestrator); N-MAT не убирает карточку с рыночными
источниками (колонка market_sources, WS_FINAL_MATURE_RESEARCH_ONLY; отключается `--any-sources`) и карточку с
уверенностью ниже `--tau` (WS_FINAL_MATURE_MIN_CONFIDENCE). Прогон — часть id до «-<тема>-<ранг>».
`--sweep` перебирает порог: выбирается наименьший, при котором на dev-прогонах потеряно не больше 5 % слабых
(правило задано до замера).
Слабые — вердикт разметки «слабый сигнал» или «ранний». `--dev-runs` — прогоны, на которых правили промпт: они
показываются отдельно и в проверочный итог не входят.

Правило внедрения (задано до замера): на проверочных прогонах потеряно не больше 10 % слабых — в сумме и в каждом
прогоне; зрелых убрано не меньше половины. Карточки без вердикта считаются оставленными (так работает orchestrator).
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

FINAL_DROP = frozenset({"N-GEN", "N-MAT", "N-OVR", "N-OFF", "N-NOI", "N-HYP", "N-FUND"})  # = orchestrator
WEAK = frozenset({"слабый сигнал", "ранний"})
MATURE = "зрелое"
MAX_WEAK_LOSS = 0.10
MIN_MATURE_CUT = 0.50


def run_of(card_id: str) -> str:
    """Имя прогона из id «<прогон>-<тема>-<ранг>»."""
    return re.sub(r"-\d+-\d+$", "", card_id)


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def is_removed(row: dict, tau: float = 0.0, research_only: bool = True) -> bool:
    """Как orchestrator: код из FINAL_DROP, кроме N-MAT у карточки с рыночными источниками или ниже порога."""
    if row["pred_code"] not in FINAL_DROP:
        return False
    if row["pred_code"] != "N-MAT":
        return True
    if research_only and str(row.get("market_sources", "0")) == "1":
        return False
    return float(row.get("confidence") or 0.0) >= tau


def tally(rows: list[dict], tau: float = 0.0, research_only: bool = True) -> dict[str, int]:
    """Счётчики: всего, зрелых, слабых и сколько из них убрано."""
    count = collections.Counter()
    for row in rows:
        kind, removed = row["label_kind"], is_removed(row, tau, research_only)
        group = "mature" if kind == MATURE else "weak" if kind in WEAK else "other"
        count["all"] += 1
        count[group] += 1
        count[group + "_removed"] += removed
        count["all_removed"] += removed
        count["unjudged"] += not row["pred_code"]
        count["cards"] += 1
    return count


def share(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else f"{part}/0"


def line(name: str, c: dict[str, int]) -> str:
    before = c["mature"] / c["all"] if c["all"] else 0.0
    left = c["all"] - c["all_removed"]
    after = (c["mature"] - c["mature_removed"]) / left if left else 0.0
    return (f"{name:28} зрелых убрано {share(c['mature_removed'], c['mature']):14} слабых потеряно "
            f"{share(c['weak_removed'], c['weak']):13} прочих убрано {share(c['other_removed'], c['other']):13} "
            f"доля зрелых {before:.1%} → {after:.1%}; без вердикта {c['unjudged']}")


def verdict(per_run: dict[str, dict[str, int]], dev: set[str]) -> tuple[str, dict[str, int]]:
    """Итог правила: слабые (главное ограничение) и доля убранных зрелых (цель) — по отдельности и вместе."""
    check = [c for run, c in per_run.items() if run not in dev]
    total = collections.Counter()
    for c in check:
        total.update(c)
    cards = sum(c["cards"] for c in check) if all("cards" in c for c in check) else 0
    unjudged = sum(c["unjudged"] for c in check)
    if check and cards and unjudged > 0.1 * cards:  # без вердиктов решение не выносится (сбой модели, а не результат)
        return (f"НЕДОСТАТОЧНО ДАННЫХ: без вердикта {unjudged} из {cards} карточек — модель не ответила; "
                "повторите замер, когда провайдер будет доступен"), collections.Counter(
                    {k: sum(c[k] for c in check) for k in check[0]})
    weak_ok = bool(check) and all(c["weak_removed"] <= MAX_WEAK_LOSS * c["weak"] for c in check)
    cut = total["mature_removed"] / total["mature"] if total["mature"] else 0.0
    parts = (f"слабые {'в допуске' if weak_ok else 'ПОТЕРИ СВЕРХ ДОПУСКА'}; зрелых убрано {cut:.0%} — цель "
             f"{'достигнута' if cut >= MIN_MATURE_CUT else 'не достигнута'}")
    return ("ВНЕДРЯТЬ" if weak_ok and cut >= MIN_MATURE_CUT else "НЕ ВНЕДРЯТЬ") + f" ({parts})", total


def sweep(per_run: dict[str, list[dict]], dev: set[str], research_only: bool = True) -> float:
    """Перебор порога: наименьший, при котором на dev-прогонах потеряно не больше 5 % слабых."""
    chosen = None
    for step in range(0, 20):
        tau = round(step * 0.05, 2)
        dev_total, check_total = collections.Counter(), collections.Counter()
        for run, items in per_run.items():
            (dev_total if run in dev else check_total).update(tally(items, tau, research_only))
        if chosen is None and dev and dev_total["weak_removed"] <= 0.05 * dev_total["weak"]:
            chosen = tau
        print(f"  τ={tau:.2f} dev: зрелых {share(dev_total['mature_removed'], dev_total['mature'])}, слабых "
              f"{share(dev_total['weak_removed'], dev_total['weak'])} | проверка: зрелых "
              f"{share(check_total['mature_removed'], check_total['mature'])}, слабых "
              f"{share(check_total['weak_removed'], check_total['weak'])}")
    chosen = chosen if chosen is not None else 0.0
    print(f"  выбран порог {chosen:.2f}" + ("" if dev else " (dev-прогоны не заданы — порог не выбирается)"))
    return chosen


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("predictions", nargs="+")
    parser.add_argument("--dev-runs", default="", help="прогоны, на которых правили промпт (через запятую)")
    parser.add_argument("--tau", type=float, default=0.0, help="порог уверенности N-MAT (как в orchestrator)")
    parser.add_argument("--sweep", action="store_true", help="перебор порога и выбор по dev-прогонам")
    parser.add_argument("--any-sources", action="store_true", help="N-MAT убирает и карточки с рыночными источниками")
    parser.add_argument("--lost", default="", help="CSV потерянных слабых сигналов и убранных зрелых")
    args = parser.parse_args(argv)
    dev = {run for run in args.dev_runs.split(",") if run}
    lost_rows: list[dict] = []
    for path in args.predictions:
        rows = load(path)
        name = Path(path).stem.replace("rubric_b_", "").replace("_predictions", "")
        per_run: dict[str, list[dict]] = collections.defaultdict(list)
        per_topic: dict[str, list[dict]] = collections.defaultdict(list)
        for row in rows:
            per_run[run_of(row["id"])].append(row)
            per_topic[row.get("topic", "")].append(row)
        print(f"\n== {name}")
        if args.sweep:
            print(f"\n== {name}: перебор порога N-MAT")
            args.tau = sweep(per_run, dev, not args.any_sources)
        counts = {run: tally(items, args.tau, not args.any_sources) for run, items in sorted(per_run.items())}
        for run, c in counts.items():
            print(line(run + (" (dev)" if run in dev else ""), c))
        for topic, items in sorted(per_topic.items()):
            print(line("  " + topic[:26], tally(items, args.tau, not args.any_sources)))
        decision, total = verdict(counts, dev)
        print(line("проверочные прогоны", total))
        print(f"порог N-MAT {args.tau:.2f}; правило: слабых потеряно ≤ {MAX_WEAK_LOSS:.0%} в каждом проверочном "
              f"прогоне, зрелых убрано ≥ {MIN_MATURE_CUT:.0%} → {decision}")
        for row in rows:
            if is_removed(row, args.tau, not args.any_sources) and (row["label_kind"] in WEAK
                                                                     or row["label_kind"] == MATURE):
                lost_rows.append({"variant": name, "run": run_of(row["id"]), "id": row["id"],
                                  "label_kind": row["label_kind"], "pred_code": row["pred_code"],
                                  "title": row.get("title", ""), "reason_ru": row.get("reason_ru", "")})
                if row["label_kind"] in WEAK:
                    print(f"  потерян слабый: {row['id']} [{row['pred_code']}] {row.get('title', '')[:90]} — "
                          f"{row.get('reason_ru', '')[:160]}")
    if args.lost:
        with open(args.lost, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["variant", "run", "id", "label_kind", "pred_code", "title",
                                                        "reason_ru"])
            writer.writeheader()
            writer.writerows(lost_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
