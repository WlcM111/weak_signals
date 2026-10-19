"""Опыт R-022(b)/R-102 (этап 5.3): размеченный набор карточек нескольких прогонов для проверки «истории области».

Выход: `<out>/labeled.jsonl` — все размеченные карточки (прогон, вердикт, только научные ли источники) и
`<out>/research.jsonl` — карточки только с научными, патентными и кодовыми источниками: правило зрелости по истории
области применяется только к ним, а карточки с рыночными источниками (СМИ, пресс-релизы) не трогаются вовсе.

Использование: python3 build_maturity_set.py <out> имя:карточки:вердикты.csv [имя:карточки:вердикты.csv ...]
Карточки — JSONL замера e2e (`strategy_check_e2e_cards.jsonl`) или JSON выгрузки заданий с полем items.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

MARKET = {"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT", "CORPORATE_BLOG", "GOVERNMENT"}


def load_cards(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    data = json.load(open(path, encoding="utf-8"))
    return [{"topic": topic["topic"], **item} for topic in data for item in topic.get("items", [])]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    labeled, research = [], []
    for spec in sys.argv[2:]:
        run, cards_path, verdicts_path = spec.split(":", 2)
        verdicts = {(r["topic"].strip(), r["title_ru"].strip()): r["verdict"].strip()
                    for r in csv.DictReader(open(verdicts_path, encoding="utf-8-sig"))}
        for card in load_cards(cards_path):
            verdict = verdicts.get((card["topic"].strip(), card.get("title_ru", "").strip()))
            if verdict is None:
                continue
            sources = card.get("sources") or []
            types = {s.get("source_type") or "" for s in sources}
            row = {"id": f"{run}-{len(labeled)}", "run": run, "topic": card["topic"], "rank": card.get("rank"),
                   "title_ru": card.get("title_ru", ""), "description_ru": card.get("description_ru") or "",
                   "sources": [s.get("title") or "" for s in sources], "verdict": verdict,
                   "research_only": bool(sources) and not (types & MARKET)}
            labeled.append(row)
            if row["research_only"]:
                research.append(row)
    for name, rows in (("labeled.jsonl", labeled), ("research.jsonl", research)):
        with open(out / name, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    by_run: dict[str, dict[str, int]] = {}
    for row in labeled:
        stats = by_run.setdefault(row["run"], {"карточек": 0, "зрелых": 0, "слабых": 0, "зрелых_научных": 0, "слабых_научных": 0})
        stats["карточек"] += 1
        weak, mature = row["verdict"] in ("слабый сигнал", "ранний"), row["verdict"] == "зрелое"
        stats["зрелых"] += mature
        stats["слабых"] += weak
        stats["зрелых_научных"] += mature and row["research_only"]
        stats["слабых_научных"] += weak and row["research_only"]
    for run, stats in by_run.items():
        print(run, stats)
    print(f"всего размечено: {len(labeled)}, только научные источники: {len(research)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
