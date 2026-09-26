"""Полнота карточек выдачи (шаг 4): доля сгенерированных карточек, шаблонные названия, пустые описания,
карточки рубричного отбора со стадией, трендом и компаниями — по файлам own-topics-*.json (tools/quality/run_topics.py).

Запуск: python3 tools/quality/card_completeness.py own-topics-*.json [--out ml/reports/rubric/card_completeness.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def card_stats(cards: list[dict]) -> dict:
    total = len(cards)

    def share(count: int) -> float | None:
        return round(count / total, 3) if total else None

    generated = sum(1 for card in cards if card.get("narrative_status") == "GENERATED")
    template = sum(1 for card in cards if str(card.get("title_ru", "")).startswith("Технология:"))
    empty = sum(1 for card in cards if not str(card.get("description_ru", "")).strip())
    rubric = sum(1 for card in cards if "Рубрика:" in str(card.get("decision_explanation_ru", "")))
    staged = sum(1 for card in cards if any(f.get("feature_name") == "stage" for f in card.get("features") or []))
    companies = sum(1 for card in cards if "Компании и организации" in str(card.get("explanation_ru", "")))
    return {"cards": total, "generated": generated, "generated_share": share(generated),
            "template_titles": template, "empty_descriptions": empty, "rubric_cards": rubric,
            "with_stage_trend": staged, "with_companies": companies}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="полнота карточек выдачи")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = {}
    for name in args.files:
        cards = [card for block in json.loads(Path(name).read_text(encoding="utf-8")) for card in block.get("cards") or []]
        report[Path(name).name] = card_stats(cards)
        print(Path(name).name, report[Path(name).name])
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
