"""U-05: повторы карточек одной компании в выдаче — два правила распознавания на одном наборе карточек.

1) «скобка»: одинаковый текст в скобках в конце названия (предложение U-05);
2) «имя»: общее собственное имя латиницей в названии (слово с заглавной буквы, не из списка общих терминов).
Вход — JSONL карточек замера (`strategy_check_e2e_cards.jsonl`). Выход — группы внутри темы для ручной сверки: правило
считается пригодным, только если все его группы — один сигнал (ложных объединений нет) и оно находит известные повторы.
"""

from __future__ import annotations

import collections
import json
import re
import sys

GENERIC = frozenset("""AI ML LLM LLMs GPU GPUs CPU SoC IoT API APIs Edge Physical Platform Platforms Agent Agents
Security Cloud Data Open Source The A An And For Of In On With New Inc Ltd LLC GmbH Corp Series Seed SaaS SDK""".split())
NAME = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*\b")


def names(title: str) -> set[str]:
    return {token for token in NAME.findall(title) if token not in GENERIC and len(token) > 2}


def main(path: str) -> int:
    cards = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    by_bracket, by_name = collections.defaultdict(list), collections.defaultdict(list)
    for card in cards:
        title = card.get("title_ru") or ""
        bracket = re.search(r"\(([^()]*)\)\s*$", title)
        if bracket:
            by_bracket[(card["topic"], bracket.group(1).strip().lower())].append(card["rank"])
        for token in names(title):
            by_name[(card["topic"], token)].append(card["rank"])
    print(f"карточек: {len(cards)}")
    for label, groups in (("скобка", by_bracket), ("имя", by_name)):
        multi = {key: ranks for key, ranks in groups.items() if len(ranks) > 1}
        print(f"\n== правило «{label}»: групп с повтором {len(multi)}")
        for (topic, key), ranks in sorted(multi.items()):
            print(f"  {topic[:40]} | {key} | ранги {ranks}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("использование: python3 tools/diagnostics/u05_name_duplicates.py <strategy_check_e2e_cards.jsonl>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
