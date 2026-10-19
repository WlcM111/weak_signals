"""Опыт R-022(b)/R-102 (этап 5.3): отсеивает ли «история области» зрелые темы, не задевая слабые сигналы.

Правило применяется только к карточкам с научными, патентными и кодовыми источниками: «зрелое», если термин
(ключевая технология `core`, задача `task` или оба) давно и широко публикуется — первый год, когда работ по термину
набралось не меньше 10, не позже Y, и всего работ не меньше T. Карточки с рыночными источниками не трогаются.

Честная проверка: правило (Y, T, какой термин) подбирается на одних прогонах так, чтобы терять не больше 3 % слабых,
и проверяется на другом (прогоны, где зрелых не меньше 10). Правило внедрения (заранее): в среднем по проверочным
прогонам убрано не меньше 50 % зрелых при потере не больше 5 % слабых (слабые — «слабый сигнал» и «ранний»).

Итерация 2 (`--mode backoff`): термин заменяется самым длинным своим «хвостом» (не короче двух слов), по которому в
OpenAlex не меньше 30 работ, а вместо «первого года» используется доля работ последних лет (2023+): старые имена
вроде «large language models» встречались и до LLM, и «первый год» их старит. Правило: «зрелое», если работ по
термину не меньше T и доля работ 2023+ не больше R. Сетка и правило внедрения — те же, заданы заранее.

Использование: python3 maturity_history_eval.py labeled.jsonl terms.jsonl history.json [--mode backoff] [--out отчёт.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys

WEAK, MATURE = {"слабый сигнал", "ранний"}, "зрелое"
FIRST_K = 10
YEARS, TOTALS, WHICH = (2005, 2010, 2014, 2017, 2019), (100, 300, 1000, 3000, 10000), ("core", "task", "both")
RECENT, BACKOFF_MIN_WORKS = (0.3, 0.4, 0.5, 0.6), 30
MODE = "first_year"
TRAIN_WEAK_LOSS, MIN_CUT, MAX_LOSS = 0.03, 0.5, 0.05


def profile(counts: dict) -> tuple[int | None, int, float]:
    if not counts or "error" in counts:
        return None, 0, 1.0
    years = sorted((int(y), n) for y, n in counts.items())
    first = next((y for y, n in years if n >= FIRST_K), None)
    total = sum(n for _, n in years)
    recent = sum(n for y, n in years if y >= 2023) / total if total else 1.0
    return first, total, recent


def resolve(phrase: str, history: dict) -> str:
    """Самый длинный «хвост» термина (не короче двух слов) с историей не меньше BACKOFF_MIN_WORKS работ."""
    words = phrase.strip().lower().split()
    for i in range(0, max(1, len(words) - 1)):
        tail = " ".join(words[i:])
        if profile(history.get(tail, {}))[1] >= BACKOFF_MIN_WORKS:
            return tail
    return " ".join(words)


def mature_by(rule: tuple[int, int, str], card: dict) -> bool:
    year, total, which = rule
    if not card["research_only"]:
        return False
    def old(term: str) -> bool:
        first, count, recent = card["history"].get(term, (None, 0, 1.0))
        if MODE == "backoff":  # year здесь — верхняя граница доли работ 2023+
            return count >= total and recent <= year
        return first is not None and first <= year and count >= total
    if which == "both":
        return old("core") and old("task")
    return old(which)


def metrics(rule: tuple[int, int, str], cards: list[dict]) -> tuple[int, int, int, int]:
    mature = [c for c in cards if c["verdict"] == MATURE]
    weak = [c for c in cards if c["verdict"] in WEAK]
    return (sum(mature_by(rule, c) for c in mature), len(mature), sum(mature_by(rule, c) for c in weak), len(weak))


def choose(cards: list[dict]) -> tuple[int, int, str]:
    best, key = None, None
    for rule in itertools.product(RECENT if MODE == "backoff" else YEARS, TOTALS, WHICH):
        cut, n_mature, lost, n_weak = metrics(rule, cards)
        if n_weak and lost / n_weak > TRAIN_WEAK_LOSS:
            continue
        candidate_key = (cut, -lost, -rule[0])
        if key is None or candidate_key > key:
            best, key = rule, candidate_key
    return best or (1900, 10**9, "both")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labeled")
    parser.add_argument("terms")
    parser.add_argument("history")
    parser.add_argument("--out", default="maturity_history_report.json")
    parser.add_argument("--mode", choices=("first_year", "backoff"), default="first_year")
    args = parser.parse_args()
    global MODE  # noqa: PLW0603 - режим опыта задаётся один раз при запуске
    MODE = args.mode
    terms = {json.loads(l)["id"]: json.loads(l) for l in open(args.terms, encoding="utf-8") if l.strip()}
    history = json.load(open(args.history, encoding="utf-8"))
    cards = []
    for line in open(args.labeled, encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        t = terms.get(row["id"], {})
        row["terms"] = {"core": t.get("core_en", ""), "task": t.get("task_en", "")}
        if MODE == "backoff":
            row["resolved"] = {k: resolve(v, history) for k, v in row["terms"].items() if v}
            row["history"] = {k: profile(history.get(v, {})) for k, v in row["resolved"].items()}
        else:
            row["history"] = {k: profile(history.get(v.strip().lower(), {})) for k, v in row["terms"].items() if v}
        cards.append(row)
    missing = sum(1 for c in cards if c["research_only"] and not c["history"])
    runs = sorted({c["run"] for c in cards})
    test_runs = [r for r in runs if sum(1 for c in cards if c["run"] == r and c["verdict"] == MATURE) >= 10]
    print(f"карточек: {len(cards)}; научных без терминов или истории: {missing}; проверочные прогоны: {test_runs}")
    folds, cuts, losses = [], [], []
    for run in test_runs:
        rule = choose([c for c in cards if c["run"] != run])
        cut, n_mature, lost, n_weak = metrics(rule, [c for c in cards if c["run"] == run])
        cuts.append(cut / n_mature)
        losses.append(lost / n_weak)
        folds.append({"run": run, "rule": {"first_year_max": rule[0], "works_min": rule[1], "term": rule[2]},
                      "mature_removed": f"{cut}/{n_mature}", "weak_lost": f"{lost}/{n_weak}"})
        limit = f"доля работ 2023+ ≤ {rule[0]}" if MODE == "backoff" else f"первый год ≤ {rule[0]}"
        print(f"проверка на {run}: правило «{rule[2]}: {limit}, работ ≥ {rule[1]}» — зрелых убрано "
              f"{cut} из {n_mature} ({cut / n_mature:.0%}), слабых задето {lost} из {n_weak} ({lost / n_weak:.0%})")
    mean_cut = sum(cuts) / len(cuts) if cuts else 0.0
    mean_loss = sum(losses) / len(losses) if losses else 1.0
    decision = "ВНЕДРЯТЬ" if cuts and mean_cut >= MIN_CUT and mean_loss <= MAX_LOSS else "НЕ ВНЕДРЯТЬ"
    print(f"\nИТОГ: в среднем зрелых убрано {mean_cut:.0%}, слабых задето {mean_loss:.0%} → {decision}")
    final = choose(cards)
    cut, n_mature, lost, n_weak = metrics(final, cards)
    limit = f"доля работ 2023+ ≤ {final[0]}" if MODE == "backoff" else f"первый год ≤ {final[0]}"
    print(f"\nПравило на всех прогонах: «{final[2]}: {limit}, работ ≥ {final[1]}» — зрелых {cut}/{n_mature},"
          f" слабых задето {lost}/{n_weak}")
    print("Слабые сигналы, которые это правило убрало бы:")
    for c in cards:
        if c["verdict"] in WEAK and mature_by(final, c):
            print(f"  [{c['run']}] {c['title_ru'][:70]} | {c.get('resolved', c['terms'])} | {c['history']}")
    print("Зрелые научные, которые правило оставило бы (первые 15):")
    for c in [c for c in cards if c["verdict"] == MATURE and c["research_only"] and not mature_by(final, c)][:15]:
        print(f"  [{c['run']}] {c['title_ru'][:70]} | {c.get('resolved', c['terms'])} | {c['history']}")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"mode": MODE, "folds": folds, "mean_mature_removed": mean_cut, "mean_weak_lost": mean_loss, "decision": decision,
                   "rule_all_runs": {"first_year_max": final[0], "works_min": final[1], "term": final[2]}},
                  handle, ensure_ascii=False, indent=1)
    print(f"отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
