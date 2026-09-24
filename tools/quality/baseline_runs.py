#!/usr/bin/env python3
"""Сводная таблица прогонов: по каждому отчёту analytics-*.txt и demo-*.txt и каждой из шести тем.

Читает только текст отчётов, ничего не запускает. Результат: CSV для машинной обработки и
Markdown-таблица для docs/quality/BASELINE_RUNS.md. Прогон или тема без итоговой выдачи
(FAILED, прерванный файл) помечаются и в метрики успешной выдачи не входят.

Запуск из корня репозитория: `python3 tools/quality/baseline_runs.py`.
"""

from __future__ import annotations

import csv
import re
import statistics
import sys
from pathlib import Path

OUT_CSV = Path("docs/quality/baseline_runs.csv")
OUT_MD = Path("docs/quality/baseline_runs_table.md")
FIELDS = [
    "run", "kind", "complete", "top_n", "cluster_threshold", "min_doc_query_sim", "min_candidate_query_sim",
    "trust_weighted", "require_high_trust", "model_ready", "theme", "status", "documents", "clusters",
    "candidates", "weak_signals", "confident", "items", "off_topic", "generated", "fallback",
    "score_min", "score_median", "score_max", "items_with_high_trust", "items_github_only",
    "sources_failed", "valid_for_metrics",
]


def _config(text: str, name: str) -> str:
    match = re.search(rf"analyzer\.{name} = (\S+)", text)
    return match.group(1) if match else ""


def parse_analytics(path: Path) -> list[dict[str, object]]:
    """Строки таблицы по отчёту скрипта theme_analytics.py."""
    text = path.read_text(encoding="utf-8")
    header = {
        "run": path.name, "kind": "analytics",
        "complete": "СВОДКА ПО ТЕМАМ" in text,
        "top_n": (re.search(r"top_n = (\d+)", text) or [None, ""])[1],
        "cluster_threshold": _config(text, "WS_CLUSTER_DISTANCE_THRESHOLD"),
        "min_doc_query_sim": _config(text, "WS_MIN_DOC_QUERY_SIM"),
        "min_candidate_query_sim": _config(text, "WS_MIN_CANDIDATE_QUERY_SIM"),
        "trust_weighted": _config(text, "WS_TRUST_WEIGHTED_SELECTION"),
        "require_high_trust": _config(text, "WS_REQUIRE_HIGH_TRUST_SOURCE"),
        "model_ready": "модель (/api/v1/model): код 200" in text,
    }
    rows = []
    for block in re.split(r"\n(?=ТЕМА [1-6]/6)", text)[1:]:
        theme = block.splitlines()[0].split("—")[0].split(":", 1)[-1].strip()
        status = (re.search(r"итоговый статус: (\w+)", block) or [None, "нет данных"])[1]
        stats = dict(re.findall(r"^  (\w+): (\S+)$", block, re.M))
        items = [b for b in re.split(r"\n(?=  #\d+ \[[A-Z_]+ \|)", block)[1:]]
        scores = [float(s) for s in re.findall(r"\n  #\d+ \[[A-Z_]+ \| \w+ \| скоринг\s+([\d.]+)%\]", block)]
        failed = re.findall(r"^  (\w+) \| FAILED \|", block, re.M)
        row = dict(header)
        row.update({
            "theme": theme, "status": status,
            "documents": stats.get("documents_collected", ""), "clusters": stats.get("clusters_total", ""),
            "candidates": stats.get("candidates_found", ""), "weak_signals": stats.get("weak_signals_total", ""),
            "confident": stats.get("weak_signals_confident", ""), "items": len(scores),
            "off_topic": stats.get("excluded_off_topic", ""),
            "generated": stats.get("narratives_generated", ""), "fallback": stats.get("narratives_fallback", ""),
            "score_min": min(scores) if scores else "", "score_max": max(scores) if scores else "",
            "score_median": round(statistics.median(scores), 1) if scores else "",
            "items_with_high_trust": sum(1 for b in items if re.search(r"- \[[a-z_]+ \| [A-Z_]+ \| HIGH \|", b)),
            "items_github_only": sum(1 for b in items if set(re.findall(r"- \[([a-z_]+) \|", b)) == {"github"}),
            "sources_failed": ",".join(failed),
            "valid_for_metrics": bool(header["complete"]) and status in ("COMPLETED", "PARTIAL") and bool(scores),
        })
        rows.append(row)
    return rows


def parse_demo(path: Path) -> list[dict[str, object]]:
    """Строки таблицы по отчётам ручных прогонов demo-*.txt (другой формат, меньше полей)."""
    # Заголовки тем в этих файлах записаны через `print -r`, поэтому начинаются с буквальных «\\n».
    text = path.read_text(encoding="utf-8").replace("\\n", "\n")
    rows = []
    for block in re.split(r"\n(?=═+ )", text)[1:]:
        theme = block.splitlines()[0].strip("═ ").strip()
        line = re.search(
            r"статус:? (\w+) \| (?:документов:? (\d+) \| )?кандидатов:? (\d+) \| сигналов:? (\d+)"
            r"(?: \| отсечено по релевантности (\d+))? \| сгенерировано:? (\d+)", block)
        scores = [float(s) for s in re.findall(r"\] +([\d.]+)%", block)]
        marks = re.findall(r"\[(M|э)\]", block)
        rows.append({
            "run": path.name, "kind": "demo", "complete": True, "top_n": 10, "theme": theme,
            "status": line.group(1) if line else "нет данных",
            "documents": line.group(2) if line and line.group(2) else "",
            "candidates": line.group(3) if line else "", "weak_signals": line.group(4) if line else "",
            "off_topic": line.group(5) if line and line.group(5) else "",
            "items": len(scores), "generated": marks.count("M"), "fallback": marks.count("э"),
            "score_min": min(scores) if scores else "", "score_max": max(scores) if scores else "",
            "score_median": round(statistics.median(scores), 1) if scores else "",
            "valid_for_metrics": bool(scores),
        })
    return rows


def main() -> int:
    reports = sorted(Path(".").glob("analytics-*.txt")) + sorted(Path(".").glob("demo-*.txt"))
    if not reports:
        print("отчёты analytics-*.txt и demo-*.txt не найдены в текущем каталоге", file=sys.stderr)
        return 2
    rows: list[dict[str, object]] = []
    for path in reports:
        rows.extend(parse_analytics(path) if path.name.startswith("analytics-") else parse_demo(path))
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    columns = ["run", "cluster_threshold", "top_n", "theme", "status", "documents", "clusters", "candidates",
               "weak_signals", "items", "generated", "fallback", "score_median", "items_with_high_trust",
               "items_github_only", "valid_for_metrics"]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    valid = [row for row in rows if row["valid_for_metrics"]]
    print(f"отчётов: {len(reports)}, строк: {len(rows)}, пригодных для метрик: {len(valid)}")
    print(f"записано: {OUT_CSV}, {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())