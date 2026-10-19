"""U-04: события предотбора analyzer (`analysis.preselect`): сколько кластеров, лучшая оценка, сколько кластеров с ней
(насыщение) и доля мест по модели. Вход — журнал analyzer на stdin."""

from __future__ import annotations

import json
import sys


def main() -> int:
    rows = 0
    print("ts | clusters | chosen | best | at_best | model_share")
    for line in sys.stdin:
        start = line.find("{")
        if start < 0 or '"analysis.preselect"' not in line:
            continue
        try:
            event = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        print(f"{event.get('ts', '')} | {event.get('clusters')} | {event.get('chosen')} | {event.get('best')} | "
              f"{event.get('at_best', 'нет в журнале (версия до патча)')} | {event.get('model_share', 'нет (1.0)')}")
        rows += 1
    if rows == 0:
        print("НЕТ ДАННЫХ: в журнале нет событий analysis.preselect", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
