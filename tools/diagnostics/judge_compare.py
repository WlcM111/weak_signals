"""Сравнение версий судьи рубрики по ручной разметке: доля кода R в каждом классе вердикта (этап 5.2, R-013).

Использование: python3 tools/diagnostics/judge_compare.py <predictions_A.csv> <predictions_B.csv> [...]
Файлы — `rubric_b_<label>_predictions.csv` из `insight.eval_rubric`. Правило внедрения версии (из плана): зрелых с
кодом R меньше хотя бы вдвое, а слабых сигналов и ранних исследований с кодом R — не больше чем на 5 % меньше.
"""

from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

ORDER = ("слабый сигнал", "ранний", "зрелое", "шум", "заметный", "повтор")


def load(path: str) -> dict[str, collections.Counter]:
    table: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in csv.DictReader(open(path, encoding="utf-8-sig")):
        table[row["label_kind"]]["всего"] += 1
        table[row["label_kind"]]["код " + (row["pred_code"] or "—")] += 1
    return table


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    runs = [(Path(path).stem.replace("rubric_b_", "").replace("_predictions", ""), load(path)) for path in sys.argv[1:]]
    print("класс | " + " | ".join(f"{name}: R/всего" for name, _ in runs))
    for kind in ORDER:
        print(f"{kind:14} | " + " | ".join(f"{t[kind]['код R']:3}/{t[kind]['всего']:<3}" for _, t in runs))
    weak = [sum(t[k]["код R"] for k in ("слабый сигнал", "ранний")) for _, t in runs]
    mature = [t["зрелое"]["код R"] for _, t in runs]
    base_weak, base_mature = weak[0], mature[0]
    for (name, _), w, m in zip(runs[1:], weak[1:], mature[1:]):
        keep = w / base_weak if base_weak else 0.0
        cut = 1 - (m / base_mature) if base_mature else 0.0
        verdict = "ВНЕДРЯТЬ" if cut >= 0.5 and keep >= 0.95 else "НЕ ВНЕДРЯТЬ"
        print(f"\n{name} против {runs[0][0]}: слабых с R {w} из {base_weak} ({keep:.0%}), зрелых с R {m} из {base_mature} "
              f"(убрано {cut:.0%}) → {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
