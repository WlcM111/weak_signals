"""Этап 5.4: проверка второго прохода зрелости на ручной разметке и таблица CASES_BEFORE_AFTER.csv.

Вход: `labeled.jsonl` (build_maturity_set.py) и `assessments.jsonl` (python -m insight.eval_maturity_check).
Правило рабочего пути: карточка только с научными источниками отсеивается, если задача давняя и широкая
(task_established, task_since_year ≤ Y) и новизна — из множества S. Карточки с рыночными источниками не трогаются.

Честная проверка: Y и S подбираются на одних прогонах так, чтобы терять не больше 10 % слабых сигналов (допуск
владельца), и проверяются на другом (прогоны, где зрелых ≥ 10). Правило внедрения (заранее): в среднем по проверочным
прогонам убрано не меньше 50 % зрелых при потере не больше 10 % слабых сигналов («слабый сигнал» и «ранний»).

Использование: python3 maturity_check_eval.py labeled.jsonl assessments.jsonl [--out-dir каталог]
Пишет: maturity_check_report.json, CASES_BEFORE_AFTER.csv (по правилу, подобранному на всех прогонах).
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path

WEAK, MATURE = {"слабый сигнал", "ранний"}, "зрелое"
YEARS = (2000, 2005, 2010, 2015, 2018, 2020)
NOVELTY_SETS = (frozenset({"improvement"}), frozenset({"improvement", "survey"}),
                frozenset({"improvement", "survey", "news"}))
TRAIN_LOSS, MIN_CUT, MAX_LOSS = 0.10, 0.50, 0.10


def mature_by(rule: tuple[int, frozenset], card: dict) -> bool:
    year, novelty = rule
    a = card.get("assessment")
    return bool(card["research_only"] and a and a["task_established"] and a["novelty_type"] in novelty
                and 1900 < int(a["task_since_year"]) <= year)


def metrics(rule: tuple[int, frozenset], cards: list[dict]) -> tuple[int, int, int, int]:
    mature = [c for c in cards if c["verdict"] == MATURE]
    weak = [c for c in cards if c["verdict"] in WEAK]
    return sum(mature_by(rule, c) for c in mature), len(mature), sum(mature_by(rule, c) for c in weak), len(weak)


def choose(cards: list[dict]) -> tuple[int, frozenset]:
    best, key = (0, frozenset()), None
    for rule in itertools.product(YEARS, NOVELTY_SETS):
        cut, _, lost, n_weak = metrics(rule, cards)
        if n_weak and lost / n_weak > TRAIN_LOSS:
            continue
        candidate = (cut, -lost, -rule[0], -len(rule[1]))
        if key is None or candidate > key:
            best, key = rule, candidate
    return best


def label(rule: tuple[int, frozenset]) -> str:
    return f"задача с {rule[0]} г. или раньше, новизна ∈ {{{', '.join(sorted(rule[1]))}}}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labeled")
    parser.add_argument("assessments")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    assessed = {json.loads(l)["key"]: json.loads(l) for l in open(args.assessments, encoding="utf-8") if l.strip()}
    cards = []
    for line in open(args.labeled, encoding="utf-8"):
        if line.strip():
            row = json.loads(line)
            row["assessment"] = assessed.get(row["id"])
            cards.append(row)
    research = [c for c in cards if c["research_only"]]
    coverage = sum(1 for c in research if c["assessment"]) / len(research) if research else 0.0
    runs = sorted({c["run"] for c in cards})
    tests = [r for r in runs if sum(1 for c in cards if c["run"] == r and c["verdict"] == MATURE) >= 10]
    print(f"карточек: {len(cards)}; с научными источниками: {len(research)}; оценено моделью: {coverage:.0%}; "
          f"проверочные прогоны: {tests}")
    folds, cuts, losses = [], [], []
    for run in tests:
        rule = choose([c for c in cards if c["run"] != run])
        cut, n_m, lost, n_w = metrics(rule, [c for c in cards if c["run"] == run])
        cuts.append(cut / n_m)
        losses.append(lost / n_w)
        folds.append({"run": run, "rule": label(rule), "mature_removed": f"{cut}/{n_m}", "weak_lost": f"{lost}/{n_w}"})
        print(f"проверка на {run}: правило «{label(rule)}» — зрелых убрано {cut} из {n_m} ({cut / n_m:.0%}), "
              f"слабых сигналов потеряно {lost} из {n_w} ({lost / n_w:.0%})")
    mean_cut = sum(cuts) / len(cuts) if cuts else 0.0
    mean_loss = sum(losses) / len(losses) if losses else 1.0
    decision = "ВНЕДРЯТЬ" if cuts and mean_cut >= MIN_CUT and mean_loss <= MAX_LOSS else "НЕ ВНЕДРЯТЬ"
    print(f"\nИТОГ: в среднем зрелых убрано {mean_cut:.0%}, слабых сигналов потеряно {mean_loss:.0%} → {decision}")
    final = choose(cards)
    cut, n_m, lost, n_w = metrics(final, cards)
    print(f"\nПравило на всех прогонах (для рабочего пути): «{label(final)}» — зрелых {cut}/{n_m}, слабых потеряно {lost}/{n_w}")
    print(f"Настройки: WS_MATURITY_CHECK_MAX_YEAR={final[0]} WS_MATURITY_CHECK_NOVELTY={','.join(sorted(final[1]))}")
    print("\nПотерянные слабые сигналы (поимённо):")
    for c in cards:
        if c["verdict"] in WEAK and mature_by(final, c):
            a = c["assessment"]
            print(f"  [{c['run']}] {c['title_ru'][:80]} | задача «{a['task_en']}» с {a['task_since_year']}, {a['novelty_type']}")
    print("\nОставшиеся зрелые с научными источниками (первые 20):")
    for c in [c for c in research if c["verdict"] == MATURE and not mature_by(final, c)][:20]:
        a = c["assessment"] or {}
        print(f"  [{c['run']}] {c['title_ru'][:80]} | {a.get('task_en', 'не оценено')} с {a.get('task_since_year', '—')}, "
              f"{'широкая' if a.get('task_established') else 'не широкая'}, {a.get('novelty_type', '—')}")
    with open(out / "CASES_BEFORE_AFTER.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["run", "topic", "rank", "card_id", "title_ru", "label", "research_only", "task_en",
                         "task_since_year", "task_established", "novelty_type", "in_output_before", "in_output_after",
                         "weak_signal_lost", "mature_removed", "rule", "status"])
        for c in cards:
            a = c["assessment"] or {}
            removed = mature_by(final, c)
            writer.writerow([c["run"], c["topic"], c.get("rank"), c["id"], c["title_ru"], c["verdict"],
                             c["research_only"], a.get("task_en", ""), a.get("task_since_year", ""),
                             a.get("task_established", ""), a.get("novelty_type", ""), True, not removed,
                             removed and c["verdict"] in WEAK, removed and c["verdict"] == MATURE, label(final),
                             "оценено" if a or not c["research_only"] else "не оценено моделью"])
    with open(out / "maturity_check_report.json", "w", encoding="utf-8") as handle:
        json.dump({"coverage": coverage, "folds": folds, "mean_mature_removed": mean_cut, "mean_weak_lost": mean_loss,
                   "decision": decision, "rule_all_runs": {"max_year": final[0], "novelty": sorted(final[1])},
                   "all_runs": {"mature_removed": f"{cut}/{n_m}", "weak_lost": f"{lost}/{n_w}"}},
                  handle, ensure_ascii=False, indent=1)
    print(f"\nфайлы: {out / 'maturity_check_report.json'}, {out / 'CASES_BEFORE_AFTER.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
