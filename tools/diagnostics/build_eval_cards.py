"""Датасет для `insight.eval_rubric --dataset b` из карточек замера e2e и ручной разметки (этап 5.2, R-013).

Судья в рабочем режиме видит фрагменты источников, поэтому фрагмент первого источника берётся из текста собранного
документа (`strategy_check_documents.jsonl`) по ссылке; без него судья видит только заголовки, и новости о раундах
выглядят «рядовыми новостями о финансировании». Метка: 1 — «слабый сигнал» или «ранний» (ранние исследования —
слабые сигналы), 0 — остальное; `label_kind` — исходный вердикт. Разбиение dev/holdout — по хэшу id (стабильно).

Использование: python3 tools/diagnostics/build_eval_cards.py <каталог_e2e> <вердикты.csv> <выход.jsonl>
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import sys
from pathlib import Path

TOPICS = (("индустриальн", "industrial_ai"), ("робототехник", "robotics"), ("инфраструктур", "ai_infrastructure"),
          ("финтех", "fintech"), ("безопасност", "ai_security"), ("edge", "edge"))
WEAK = {"слабый сигнал", "ранний"}


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    folder, verdicts_path, out_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    verdicts = {(r["topic"].strip(), r["title_ru"].strip()): r["verdict"].strip()
                for r in csv.DictReader(open(verdicts_path, encoding="utf-8-sig"))}
    texts = {}
    for line in open(folder / "strategy_check_documents.jsonl", encoding="utf-8"):
        document = json.loads(line)
        texts.setdefault(document["url"], document.get("text") or document.get("title") or "")
    rows, kinds, missing, with_text = [], collections.Counter(), 0, 0
    for line in open(folder / "strategy_check_e2e_cards.jsonl", encoding="utf-8"):
        card = json.loads(line)
        verdict = verdicts.get((card["topic"].strip(), card["title_ru"].strip()))
        if verdict is None:
            missing += 1
            continue
        index = next(i for i, (key, _) in enumerate(TOPICS) if key in card["topic"].lower())
        group_id = f"{folder.name}-{index}-{card['rank']}"
        sources = card.get("sources") or []
        snippet = next((texts[s["url"]] for s in sources if texts.get(s.get("url") or "")), "")
        with_text += bool(snippet)
        rows.append({"group_id": group_id, "title": card["title_ru"], "description": card.get("description_ru") or "",
                     "label": 1 if verdict in WEAK else 0,
                     "label_kind": verdict, "domain_tag": TOPICS[index][1], "topic": card["topic"],
                     "split": "holdout" if int(hashlib.sha1(group_id.encode()).hexdigest(), 16) % 5 == 0 else "dev",
                     "observation_text": (snippet or " | ".join(s.get("title") or "" for s in sources))[:400],
                     "evidence_refs": [{"title": s.get("title") or "", "url": s.get("url") or "",
                                        "source_type": s.get("source_type") or "", "trust_level": "", "source_key": "",
                                        "published_at": "", "language_code": ""} for s in sources]})
        kinds[verdict] += 1
    with open(out_path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"карточек: {len(rows)} (без разметки: {missing}; с фрагментом текста источника: {with_text}) {dict(kinds)}")
    return 0 if rows else 3


if __name__ == "__main__":
    sys.exit(main())
