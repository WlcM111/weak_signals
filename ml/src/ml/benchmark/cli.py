"""Команды режима А: проверка снимков, повтор, воронка, сопоставление с экспертами, парное сравнение.

  python -m ml.benchmark verify  СНИМКИ…
  python -m ml.benchmark replay  СНИМКИ… --out КАТАЛОГ [--set analyzer.min_candidate_query_sim=0.85 …] [--repeat 2]
  python -m ml.benchmark funnel  СНИМКИ… --out КАТАЛОГ
  python -m ml.benchmark synonyms-template --dataset ДАТАСЕТ --out ФАЙЛ.yaml
  python -m ml.benchmark match   СНИМКИ… --dataset ДАТАСЕТ [--synonyms ФАЙЛ.yaml] --out КАТАЛОГ
  python -m ml.benchmark match-score --dir КАТАЛОГ
  python -m ml.benchmark compare A.json B.json --metric recall.shown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ml.benchmark import expert_match, funnel, replay, stats
from ml.benchmark.snapshot import find_snapshots


def _dump(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.benchmark", description="режим А: повторяемая оценка выдачи")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("verify")
    p.add_argument("snapshots", nargs="+")
    p = sub.add_parser("replay")
    p.add_argument("snapshots", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--set", dest="overrides", action="append", default=[])
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--recompute-analysis", action="store_true",
                   help="рубрика и доводка от пересчитанного анализа, а не от кандидатов живого задания")
    p = sub.add_parser("funnel")
    p.add_argument("snapshots", nargs="+")
    p.add_argument("--out", required=True)
    p = sub.add_parser("synonyms-template")
    p.add_argument("--dataset", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("match")
    p.add_argument("snapshots", nargs="+")
    p.add_argument("--dataset", required=True)
    p.add_argument("--synonyms", default="")
    p.add_argument("--out", required=True)
    p = sub.add_parser("match-score")
    p.add_argument("--dir", required=True)
    p = sub.add_parser("compare")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--metric", required=True, help="recall.<этап> для match_report.json или precision_at_<K> для human_eval")
    args = parser.parse_args(argv)
    snapshots = find_snapshots(args.snapshots) if hasattr(args, "snapshots") else []
    if hasattr(args, "snapshots") and not snapshots:
        # Пустой ввод — ошибка, а не успех: иначе проверка «проходит» без единого снимка.
        print(f"снимки не найдены: {' '.join(args.snapshots)} — выгрузите их tools/benchmark/freeze.py", file=sys.stderr)
        return 2

    if args.command == "verify":
        failed = 0
        for snapshot in snapshots:
            problems = snapshot.verify()
            failed += bool(problems)
            print(f"{snapshot.name}: {'OK' if not problems else '; '.join(problems)}")
        return 1 if failed else 0
    if args.command == "replay":
        overrides = replay.parse_overrides(args.overrides)
        summary, failed = [], 0
        for snapshot in snapshots:
            results = [replay.replay(snapshot, overrides, recompute=args.recompute_analysis) for _ in range(max(1, args.repeat))]
            first = results[0]
            stable = all(r.digest == first.digest and r.status == first.status for r in results)
            _dump(Path(args.out) / f"replay-{snapshot.name}.json", first.as_dict() | {"repeats": len(results), "repeat_stable": stable})
            summary.append({"snapshot": snapshot.name, "topic": snapshot.topic, "status": first.status,
                            "repeat_stable": stable, "cards_live": len(first.live_items), "cards_replay": len(first.items),
                            "misses": {k: first.misses.get(k) for k in ("llm", "embeddings", "encyclopedia")},
                            "differences": len(first.differences), "issues": first.issues,
                            "analysis_mode": first.analysis_mode,
                            "analysis_reproducible": first.analysis_check.get("reproducible"),
                            "analysis_differences": first.analysis_check.get("differences", [])[:10]})
            failed += (first.status != replay.IDENTICAL and not overrides) or not stable
            print(f"{snapshot.name}: {first.status}, повторов {len(results)}, совпадают между собой: {stable}, "
                  f"карточек живое/повтор {len(first.live_items)}/{len(first.items)}, без записи {first.misses}, "
                  f"кандидаты: {first.analysis_mode}, пересчёт анализа совпал: {first.analysis_check.get('reproducible')}")
        _dump(Path(args.out) / "replay_summary.json", {"overrides": overrides, "runs": summary})
        return 1 if failed else 0
    if args.command == "funnel":
        reports = [funnel.funnel(snapshot) for snapshot in snapshots]
        _dump(Path(args.out) / "funnel.json", reports)
        (Path(args.out) / "funnel.md").write_text(funnel.render_markdown(reports), encoding="utf-8")
        print(funnel.render_markdown(reports))
        return 0
    rows = None
    if getattr(args, "dataset", None):
        rows = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.command == "synonyms-template":
        Path(args.out).write_text(expert_match.synonyms_template(rows or []), encoding="utf-8")
        print(f"шаблон синонимов: {args.out}")
        return 0
    if args.command == "match":
        synonyms, digest = expert_match.load_synonyms(args.synonyms)
        result = expert_match.match(snapshots, rows or [], synonyms)
        result |= {"dataset": Path(args.dataset).name, "synonyms_sha256": digest}
        out = Path(args.out)
        _dump(out / "matches.json", result)
        pairs = expert_match.review_pairs(result)
        expert_match.write_sheets(pairs, out)
        print(f"спорных пар для двух проверяющих: {len(pairs)}; листы {out}/reviewer_1.csv и reviewer_2.csv")
        return 0
    if args.command == "match-score":
        print(json.dumps(expert_match.score(Path(args.dir)), ensure_ascii=False, indent=1))
        return 0
    if args.command == "compare":
        def per_topic(path: str) -> dict[str, float]:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if args.metric.startswith("recall."):
                stage = args.metric.split(".", 1)[1]
                return {r["topic"]: r["recall"][stage]["share"] for r in data["runs"] if stage in r["recall"]}
            return {topic: value for topic, value in (data.get("by_topic", {}).get(args.metric) or {}).items() if value is not None}
        print(json.dumps(stats.paired_bootstrap(per_topic(args.a), per_topic(args.b)), ensure_ascii=False, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
