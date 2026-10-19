"""U-01: доля одиночных по двум знаменателям из журнала analyzer (`analysis.clusters`), по каждому анализу.

Вход — строки `docker compose logs analyzer` на stdin. Доля документов в одиночных кластерах = singletons / documents;
доля одиночных кластеров = singletons / total (кластер размера 1 содержит ровно один документ, поэтому числитель один).
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    rows = 0
    print("ts | threshold | mode | documents | clusters | singletons | share_by_documents | share_by_clusters | largest")
    for line in sys.stdin:
        start = line.find("{")
        if start < 0 or '"analysis.clusters"' not in line:
            continue
        try:
            event = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        total, singles, documents = event.get("total", 0), event.get("singletons", 0), event.get("documents", 0)
        by_docs = singles / documents if documents else float("nan")
        by_clusters = singles / total if total else float("nan")
        print(f"{event.get('ts', '')} | {event.get('threshold')} | {event.get('mode', 'fixed')} | {documents} | {total} | "
              f"{singles} | {by_docs:.3f} | {by_clusters:.3f} | {event.get('largest')}")
        rows += 1
    if rows == 0:
        print("НЕТ ДАННЫХ: в журнале нет событий analysis.clusters за выбранный период", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
