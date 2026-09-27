"""Попадание в слабые сигналы организаторов — основной критерий первичной проверки заказчика.

Ответ заказчика (чат задачи, 20.09 и 26.09.2026): по шести темам датасета выдача сравнивается с датасетом;
«основным критерием первичной проверки является попадание в слабые сигналы». Инструмент берёт выдачу по
шести запросам кейса (own-topics-*.json или analytics-*.txt) и 100 строк организаторов, считает косинусную
близость e5 между каждой технологией организаторов и каждой карточкой той же темы и сообщает:
- сколько технологий организаторов имеют карточку с близостью не ниже порога (несколько порогов);
- пары «технология организаторов — лучшая карточка» для ручной проверки: близость e5 — ориентир, решение
  о попадании по смыслу проверяется глазами (файл пар).

Запуск в trainer: python -m ml.hit_rate --dataset /data/labels/dataset_ds-2026.09.19-v2.jsonl
    --runs /app/ml/reports/rubric/hit/own-topics-….json --out /app/ml/reports/rubric/hit --label rubric_v2
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

DOMAIN_QUERY = {
    "industrial_ai": "индустриальный искусственный интеллект на производстве",
    "robotics": "робототехника и физический искусственный интеллект",
    "ai_infrastructure": "инфраструктура для искусственного интеллекта и дата-центры",
    "fintech": "перспективные решения в финтехе",
    "ai_security": "защита и безопасность систем искусственного интеллекта",
    "edge": "edge-вычисления и периферийный инференс",
}
THRESHOLDS = (0.80, 0.83, 0.86, 0.89)
MODEL = "intfloat/multilingual-e5-base"


def load_cards(paths: list[str]) -> dict[str, list[dict]]:
    """Карточки выдачи по запросу: own-topics JSON (название, описание) или отчёт analytics (название, источники)."""
    cards: dict[str, list[dict]] = {}
    for name in paths:
        path = Path(name)
        if path.suffix == ".json":
            for block in json.loads(path.read_text(encoding="utf-8")):
                for card in block.get("cards") or []:
                    text = f"{card.get('title_ru', '')}. {card.get('description_ru', '')}"
                    cards.setdefault(block.get("topic", ""), []).append({"title": card.get("title_ru", ""), "text": text})
        else:
            from ml.dataset_b.runs_parser import parse_analytics  # noqa: PLC0415

            for item in parse_analytics(path)[0]:
                titles = [getattr(e, "title", None) or (e.get("title", "") if isinstance(e, dict) else "")
                          for e in item.evidence[:3]]
                title = item.title_ru or item.title_auto
                text = title + ". " + "; ".join(titles)
                cards.setdefault(item.topic, []).append({"title": title, "text": text})
    return cards


def match(org_vectors: np.ndarray, card_vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Для каждой технологии организаторов — лучшая близость и индекс карточки (векторы нормированы)."""
    if not len(card_vectors):
        return np.zeros(len(org_vectors)), np.full(len(org_vectors), -1)
    sims = org_vectors @ card_vectors.T
    return sims.max(axis=1), sims.argmax(axis=1)


def summarize(domain_best: dict[str, np.ndarray], cards_per_domain: dict[str, int]) -> dict:
    report = {"by_domain": {}, "total": {}}
    orgs = sum(len(best) for best in domain_best.values())
    for domain, best in domain_best.items():
        report["by_domain"][domain] = {"organizer_signals": len(best), "cards": cards_per_domain.get(domain, 0),
                                       **{f"hits@{t:.2f}": int((best >= t).sum()) for t in THRESHOLDS},
                                       "best_mean": round(float(best.mean()), 4) if len(best) else None}
    for t in THRESHOLDS:
        hits = sum(int((best >= t).sum()) for best in domain_best.values())
        report["total"][f"hits@{t:.2f}"] = {"hits": hits, "of": orgs, "share": round(hits / orgs, 4) if orgs else None}
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="попадание выдачи в слабые сигналы организаторов")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="run")
    args = parser.parse_args(argv)
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415 - тяжёлый импорт только при запуске

    rows = [json.loads(l) for l in Path(args.dataset).read_text(encoding="utf-8").splitlines() if l.strip()]
    orgs = [r for r in rows if r.get("label") == 1]
    cards = load_cards(args.runs)
    model = SentenceTransformer(MODEL)
    embed = lambda texts: model.encode([f"query: {t}" for t in texts], normalize_embeddings=True)  # noqa: E731
    domain_best, pairs, counts = {}, [], {}
    for domain, query in DOMAIN_QUERY.items():
        items = [r for r in orgs if r.get("domain_tag") == domain]
        found = cards.get(query, [])
        counts[domain] = len(found)
        best, index = match(embed([f"{r['title']}. {r.get('description', '')}" for r in items]),
                            embed([c["text"] for c in found]) if found else np.zeros((0, 768)))
        domain_best[domain] = best
        for row, sim, idx in zip(items, best, index):
            pairs.append({"domain": domain, "organizer_title": row["title"], "best_card": found[idx]["title"] if idx >= 0 else "",
                          "similarity": round(float(sim), 4)})
    report = summarize(domain_best, counts)
    report.update({"label": args.label, "runs": [Path(r).name for r in args.runs], "model": MODEL})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"hit_rate_{args.label}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / f"hit_rate_{args.label}_pairs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["domain", "organizer_title", "best_card", "similarity"])
        writer.writeheader()
        writer.writerows(sorted(pairs, key=lambda p: (p["domain"], -p["similarity"])))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
