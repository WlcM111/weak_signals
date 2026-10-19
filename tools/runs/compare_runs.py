"""Сравнение двух прогонов шести тем (`strategy_check.py --e2e`): до и после, по каждой теме и суммарно.

Использование: python3 tools/runs/compare_runs.py <каталог_до> <каталог_после> [--verdicts ml/data/benchmark/...csv]
Каталог — распакованный результат замера (strategy_check_e2e.json, *_manifest.json, *_cards.jsonl). Неизмеренное
показывается как «не измерено», а не 0. Пишет compare_runs.csv в текущий каталог.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def load(folder: Path) -> dict:
    report = json.loads((folder / "strategy_check_e2e.json").read_text(encoding="utf-8"))
    manifest = json.loads((folder / "strategy_check_e2e_manifest.json").read_text(encoding="utf-8"))
    cards_path = folder / "strategy_check_e2e_cards.jsonl"
    cards = [json.loads(l) for l in cards_path.read_text(encoding="utf-8").splitlines() if l.strip()] if cards_path.exists() else None
    status = {t["topic"]: (t.get("job_id"), t.get("job_status")) for t in manifest.get("topics", [])}
    topics = {}
    for topic in report["topics"]:
        name = topic["topic"]
        own = [c for c in cards if c["topic"] == name] if cards is not None else None
        kinds = {}
        if own is not None:
            for card in own:
                kinds[card.get("evidence_kind") or "?"] = kinds.get(card.get("evidence_kind") or "?", 0) + 1
        topics[name] = {"job_id": status.get(name, (None, None))[0], "status": status.get(name, (None, None))[1],
                        "documents": topic.get("documents"), "candidates": topic.get("candidates"),
                        "cards": len(own) if own is not None else "не измерено", "experts_collected": topic.get("found"),
                        "experts_total": topic.get("experts"), "kinds": kinds or "не измерено"}
    return {"topics": topics, "stages": report["totals"].get("stages", "не измерено"),
            "cards": report["totals"].get("cards", "не измерено"), "weak_share": report["totals"].get("weak_share")}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    before, after = load(Path(sys.argv[1])), load(Path(sys.argv[2]))
    rows = []
    for name in sorted(set(before["topics"]) | set(after["topics"])):
        b, a = before["topics"].get(name, {}), after["topics"].get(name, {})
        rows.append([name, b.get("job_id"), a.get("job_id"), b.get("status"), a.get("status"), b.get("documents"),
                     a.get("documents"), b.get("cards"), a.get("cards"), b.get("experts_collected"),
                     a.get("experts_collected"), json.dumps(b.get("kinds"), ensure_ascii=False),
                     json.dumps(a.get("kinds"), ensure_ascii=False)])
    header = ["topic", "job_before", "job_after", "status_before", "status_after", "documents_before", "documents_after",
              "cards_before", "cards_after", "experts_collected_before", "experts_collected_after", "kinds_before",
              "kinds_after"]
    with open("compare_runs.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    for row in rows:
        print(" | ".join(str(value) for value in row))
    print("\nэтапы до:", before["stages"])
    print("этапы после:", after["stages"])
    print("доля слабых сигналов (по разметке) до/после:", before["weak_share"], after["weak_share"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
