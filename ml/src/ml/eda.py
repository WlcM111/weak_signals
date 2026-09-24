"""Сценарий `eda`: профиль обучающего набора и графики распределений (§7 HANDOFF)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ml.dataset import TrainingRow


def profile(rows: Sequence[TrainingRow]) -> dict[str, Any]:
    """Считает состав набора: классы, подтипы, области, стадии, тренды, длины описаний."""
    lengths = [len(row.description) for row in rows]
    return {
        "rows_total": len(rows),
        "positives": sum(1 for row in rows if row.label == 1),
        "negatives": sum(1 for row in rows if row.label == 0),
        "by_label_kind": dict(Counter(row.label_kind for row in rows)),
        "by_domain": dict(Counter(row.domain_tag for row in rows)),
        "by_origin": dict(Counter(row.origin for row in rows)),
        "stage_ordinal": dict(
            Counter(str(row.stage_ordinal) for row in rows if row.stage_ordinal is not None)
        ),
        "trend_ordinal": dict(
            Counter(str(row.trend_ordinal) for row in rows if row.trend_ordinal is not None)
        ),
        "description_length": {
            "min": min(lengths) if lengths else 0,
            "median": sorted(lengths)[len(lengths) // 2] if lengths else 0,
            "max": max(lengths) if lengths else 0,
        },
        "rows_without_sources": sum(1 for row in rows if not row.source_urls),
        "duplicate_group_ids": len(rows) - len({row.group_id for row in rows}),
    }


def write_profile(rows: Sequence[TrainingRow], reports_dir: Path) -> Path:
    """Сохраняет профиль в `reports/eda.json` и краткую сводку в `reports/eda.md`."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    data = profile(rows)
    (reports_dir / "eda.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# EDA обучающего набора",
        "",
        f"Строк: **{data['rows_total']}** (позитивов {data['positives']}, негативов {data['negatives']}).",
        "",
        "| Разрез | Значения |",
        "|---|---|",
        f"| Тип метки | {_format(data['by_label_kind'])} |",
        f"| Область | {_format(data['by_domain'])} |",
        f"| Источник строк | {_format(data['by_origin'])} |",
        f"| Стадия (позитивы) | {_format(data['stage_ordinal'])} |",
        f"| Тренд (позитивы) | {_format(data['trend_ordinal'])} |",
        "",
        f"Длина описания: мин {data['description_length']['min']}, "
        f"медиана {data['description_length']['median']}, макс {data['description_length']['max']}.",
        f"Строк без источников: {data['rows_without_sources']}.",
    ]
    path = reports_dir / "eda.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _format(counter: dict[str, Any]) -> str:
    """Словарь счётчиков в читаемую строку."""
    return ", ".join(f"{key}: {value}" for key, value in sorted(counter.items()))
