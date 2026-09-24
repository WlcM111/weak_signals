"""Сценарий `enrich`: ENRICHMENT-коллекции для каждой обучающей строки (§7.1 HANDOFF)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ws_common.logging import get_logger

from ml.dataset import TrainingRow

ERRORS_FILE = "_errors.jsonl"


def enrich_rows(
    rows: Sequence[TrainingRow],
    enricher: Any,
    dataset_version: str,
    output_dir: Path,
    force: bool = False,
) -> tuple[int, int]:
    """Обогащает строки, пропуская уже собранные; возвращает (собрано, ошибок).

    Идемпотентность двойная: файл `<row_id>.json` не пересобирается без `--force`, а ключ
    `<ds>:<row_id>:enrich` заставляет collector вернуть ту же коллекцию при повторе.
    """
    log = get_logger("ml.enrich")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    errors_path = output_dir / ERRORS_FILE
    collected = 0
    failed = 0
    for row in rows:
        target = output_dir / f"{row.row_id}.json"
        if target.exists() and not force:
            continue
        key = f"{dataset_version}:{row.row_id}:enrich"
        try:
            payload = enricher.enrich(key, row.title, english_title(row.title))
        except Exception as error:  # noqa: BLE001 - отказ одной строки не останавливает прогон
            failed += 1
            with errors_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"row_id": row.row_id, "error": str(error)}, ensure_ascii=False) + "\n"
                )
            log.warning("ml.enrich.row", row_id=row.row_id, status="FAILED", error=str(error))
            continue
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        collected += 1
        log.info(
            "ml.enrich.row",
            row_id=row.row_id,
            status=payload.get("status", "UNKNOWN"),
            documents=len(payload.get("documents", [])),
        )
    return collected, failed


def english_title(title: str) -> str:
    """Английская часть названия: текст в скобках, если он латиницей, иначе само название."""
    if "(" in title and ")" in title:
        inner = title[title.index("(") + 1 : title.rindex(")")].strip()
        if inner and sum(char.isascii() and char.isalpha() for char in inner) > len(inner) / 2:
            return inner
    return title


def load_enrichment(directory: Path, row_id: str) -> dict[str, Any] | None:
    """Читает сохранённую коллекцию строки, если она есть."""
    path = Path(directory) / f"{row_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
