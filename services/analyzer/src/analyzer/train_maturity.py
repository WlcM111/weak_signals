"""Обучение локальной модели зрелости (этап 5.5) — тем же способом, что прошёл проверку `maturity_cv.py`:
эмбеддинги bge-m3 текста карточки, логистическая регрессия на карточках только с научными источниками, порог —
отсекает не больше `--train-loss` (2 %) всех слабых сигналов обучающей разметки. Запуск в контейнере analyzer:

python -m analyzer.train_maturity --run имя:карточки:вердикты.csv [...] --out /models/maturity_bgem3_v1.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

WEAK, MATURE = {"слабый сигнал", "ранний"}, "зрелое"
MARKET = {"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT", "CORPORATE_BLOG", "GOVERNMENT"}


def card_text(title: str, description: str, source_titles: list[str]) -> str:
    """Текст карточки — одинаковый при обучении и в рабочем пути (orchestrator собирает его так же)."""
    sources = "; ".join(t[:120] for t in source_titles[:3])
    return f"{title}. {description[:600]} Источники: {sources}"


def load_cards(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    data = json.load(open(path, encoding="utf-8"))
    return [{"topic": topic["topic"], **item} for topic in data for item in topic.get("items", [])]


def main() -> int:
    import numpy as np  # noqa: PLC0415
    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

    from analyzer.adapters.outbound.e5_embedder import E5Embedder  # noqa: PLC0415
    from analyzer.config import AnalyzerSettings  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--train-loss", type=float, default=0.02)
    args = parser.parse_args()
    texts, labels, research, digest = [], [], [], hashlib.sha256()
    for spec in args.run:
        _, cards_path, verdicts_path = spec.split(":", 2)
        digest.update(Path(cards_path).read_bytes())
        digest.update(Path(verdicts_path).read_bytes())
        verdicts = {(r["topic"].strip(), r["title_ru"].strip()): r["verdict"].strip()
                    for r in csv.DictReader(open(verdicts_path, encoding="utf-8-sig"))}
        for card in load_cards(cards_path):
            verdict = verdicts.get((card["topic"].strip(), card.get("title_ru", "").strip()))
            if verdict not in WEAK and verdict != MATURE:
                continue
            sources = card.get("sources") or []
            types = {s.get("source_type") or "" for s in sources}
            texts.append(card_text(card.get("title_ru", ""), card.get("description_ru") or "",
                                   [s.get("title") or "" for s in sources]))
            labels.append(1 if verdict == MATURE else 0)
            research.append(bool(types) and not (types & MARKET))
    settings = AnalyzerSettings()
    vectors = np.asarray(E5Embedder(args.model, cache_dir=str(settings.hf_home)).encode(texts, args.prefix))
    y, mask = np.array(labels), np.array(research)
    model = LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000).fit(vectors[mask], y[mask])
    scores = model.predict_proba(vectors)[:, 1]
    weak_scores = np.sort(scores[(y == 0) & mask])[::-1]
    allowed = int(args.train_loss * int((y == 0).sum()))
    threshold = float(weak_scores[allowed]) if allowed < len(weak_scores) else float(weak_scores[-1]) - 1e-9
    flagged = (scores > threshold) & mask
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    payload = {"model": args.model, "prefix": args.prefix, "coef": [float(x) for x in model.coef_[0]],
               "intercept": float(model.intercept_[0]), "threshold": threshold,
               "version": f"maturity-bgem3-{stamp}-{digest.hexdigest()[:8]}",
               "trained_on": {"cards": len(labels), "research_cards": int(mask.sum()), "mature": int(y.sum()),
                              "weak": int((y == 0).sum()), "train_loss": args.train_loss,
                              "mature_flagged": int((flagged & (y == 1)).sum()),
                              "weak_flagged": int((flagged & (y == 0)).sum()), "data_sha256": digest.hexdigest()}}
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"модель: {payload['version']}; порог {threshold:.4f}; на обучающей разметке отмечено зрелых "
          f"{payload['trained_on']['mature_flagged']}/{payload['trained_on']['mature']}, слабых "
          f"{payload['trained_on']['weak_flagged']}/{payload['trained_on']['weak']} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
