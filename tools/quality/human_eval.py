"""Человеческая проверка выдачи: слепые листы для двух разметчиков и расчёт согласия и точности.

  python3 tools/quality/human_eval.py export --out ml/reports/human own-topics-….json [ещё файлы]
      → expert1.csv и expert2.csv (одинаковые, без оценок модели), manifest.json
  python3 tools/quality/human_eval.py score --dir ml/reports/human [--k 3 5 10]
      → каппа Коэна по решению «слабый сигнал или нет», точность выдачи по каждому разметчику и по согласованным
        карточкам (ml/reports/human/human_eval_report.json); precision@K по темам по согласованным карточкам
        с 95 % интервалом бутстрэпом по темам (этап 2, режим А)

Коды меток — рубрика ml/data/dataset_b/LABELING_PROTOCOL.md: R — слабый сигнал; N-OFF, N-GEN, N-OVR, N-MAT,
N-NOI, N-HYP, N-FUND — причины отказа; U — нельзя решить. Разметчики работают независимо, листы не обсуждают.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

CODES = {"R", "N-OFF", "N-GEN", "N-OVR", "N-MAT", "N-NOI", "N-HYP", "N-FUND", "U"}
FIELDS = ["card_id", "topic", "title", "description", "advantage", "case_example", "sources", "label", "comment"]


def export(files: list[str], out: Path) -> dict:
    rows = []
    for name in files:
        for index, block in enumerate(json.loads(Path(name).read_text(encoding="utf-8"))):
            for card in block.get("cards") or []:
                sources = " | ".join(f"{s.get('title', '')} ({s.get('url', '')})" for s in (card.get("sources") or [])[:6])
                rows.append({"card_id": f"{Path(name).stem}#{index}#{card.get('rank')}", "topic": block.get("topic", ""),
                             "title": card.get("title_ru", ""), "description": card.get("description_ru", ""),
                             "advantage": card.get("advantage_ru", ""), "case_example": card.get("case_example_ru", ""),
                             "sources": sources, "label": "", "comment": ""})
    out.mkdir(parents=True, exist_ok=True)
    for sheet in ("expert1.csv", "expert2.csv"):
        with (out / sheet).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    manifest = {"files": [Path(f).name for f in files], "cards": len(rows)}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def read_sheet(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["card_id"]: row["label"].strip().upper() for row in csv.DictReader(handle) if row.get("card_id")}


def kappa(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return round((po - pe) / (1 - pe), 4) if pe < 1 else None


def precision_at_k(one: dict[str, str], two: dict[str, str], topics: dict[str, str], k: int) -> dict:
    """precision@K по темам: доля R среди согласованных карточек с рангом ≤ K (несогласованные не входят)."""
    by_topic: dict[str, list[int]] = {}
    for cid, label in one.items():
        other = two.get(cid)
        if label not in CODES - {"U"} or other not in CODES - {"U"} or (label == "R") != (other == "R"):
            continue
        try:
            rank = int(cid.rsplit("#", 1)[1])
        except (IndexError, ValueError):
            continue
        if rank <= k:
            by_topic.setdefault(topics.get(cid, ""), []).append(1 if label == "R" else 0)
    values = {topic: round(sum(v) / len(v), 4) for topic, v in by_topic.items() if v}
    return {"by_topic": values, "mean_over_topics": round(sum(values.values()) / len(values), 4) if values else None,
            "ci95_bootstrap_topics": bootstrap(list(values.values()))}


def bootstrap(values: list[float], iterations: int = 2000, seed: int = 20261003) -> list[float] | None:
    """95 % перцентильный интервал среднего бутстрэпом по темам; None, если тем меньше двух."""
    import random  # noqa: PLC0415

    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(iterations))
    return [round(means[int(0.025 * iterations)], 4), round(means[int(0.975 * iterations) - 1], 4)]


def read_topics(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["card_id"]: row.get("topic", "") for row in csv.DictReader(handle) if row.get("card_id")}


def score(folder: Path, ks: tuple[int, ...] = (3, 5, 10)) -> dict:
    one, two = read_sheet(folder / "expert1.csv"), read_sheet(folder / "expert2.csv")
    bad = sorted({v for v in list(one.values()) + list(two.values()) if v and v not in CODES})
    both = [cid for cid in one if one[cid] in CODES - {"U"} and two.get(cid) in CODES - {"U"}]
    a = [1 if one[cid] == "R" else 0 for cid in both]
    b = [1 if two[cid] == "R" else 0 for cid in both]
    agreed = [x for x, y in zip(a, b) if x == y]
    report = {"cards": len(one), "labeled_by_both": len(both), "unknown_codes": bad, "kappa": kappa(a, b),
              "precision_expert1": round(sum(a) / len(a), 4) if a else None,
              "precision_expert2": round(sum(b) / len(b), 4) if b else None,
              "agreed": len(agreed), "precision_agreed": round(sum(agreed) / len(agreed), 4) if agreed else None}
    topics = read_topics(folder / "expert1.csv")
    report["by_topic"] = {}
    for k in ks:
        result = precision_at_k(one, two, topics, k)
        report[f"precision_at_{k}"] = {key: value for key, value in result.items() if key != "by_topic"}
        report["by_topic"][f"precision_at_{k}"] = result["by_topic"]
    (folder / "human_eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="человеческая проверка выдачи")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--out", required=True)
    exp.add_argument("files", nargs="+")
    sc = sub.add_parser("score")
    sc.add_argument("--dir", required=True)
    sc.add_argument("--k", type=int, nargs="+", default=[3, 5, 10])
    args = parser.parse_args(argv)
    result = export(args.files, Path(args.out)) if args.command == "export" else score(Path(args.dir), tuple(args.k))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
