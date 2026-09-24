"""Сценарий `build-negatives`: строки отрицательного класса, каппа Коэна и сборка набора (§6.2)."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ml.dataset import NEGATIVE_ORIGIN, TrainingRow, slugify

SUBTYPE_TO_KIND = {"mature": "MATURE", "hype": "HYPE_OR_NOISE", "noise": "HYPE_OR_NOISE"}
TARGET_SHARES = {"mature": 0.50, "hype": 0.30, "noise": 0.20}
MIN_NEGATIVES = 120
SHARE_TOLERANCE = 0.10


class NegativesError(RuntimeError):
    """Список кандидатов-негативов не соответствует протоколу."""


@dataclass(frozen=True, slots=True)
class NegativesReport:
    """Состав построенного отрицательного класса."""

    rows: tuple[TrainingRow, ...]
    by_subtype: dict[str, int]
    by_domain: dict[str, int]
    annotator: str
    warnings: tuple[str, ...]


def load_negatives(path: Path, start_index: int = 1) -> NegativesReport:
    """Читает YAML-список кандидатов и превращает его в строки обучающего набора."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    items = payload.get("items") or []
    annotator = str(payload.get("annotator", "dev_b"))
    if not items:
        raise NegativesError(f"{path}: список кандидатов пуст")
    rows: list[TrainingRow] = []
    by_subtype: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    seen_titles: set[str] = set()
    for offset, item in enumerate(items):
        subtype = str(item.get("subtype", "")).lower()
        if subtype not in SUBTYPE_TO_KIND:
            raise NegativesError(f"{item.get('title')!r}: неизвестный подтип {subtype!r}")
        title = str(item["title"]).strip()
        if title.lower() in seen_titles:
            raise NegativesError(f"дублирующееся название: {title}")
        seen_titles.add(title.lower())
        rows.append(
            TrainingRow(
                row_id=f"neg-{start_index + offset:04d}",
                group_id=slugify(title),
                origin=NEGATIVE_ORIGIN,
                title=title,
                description=str(item.get("description", "")).strip(),
                domain_tag=str(item["domain"]),
                label=0,
                label_kind=SUBTYPE_TO_KIND[subtype],
                source_urls=[str(item["url"])] if item.get("url") else [],
                annotators=[annotator],
                annotation_agreement=item.get("annotation_agreement"),
            )
        )
        by_subtype[subtype] = by_subtype.get(subtype, 0) + 1
        by_domain[rows[-1].domain_tag] = by_domain.get(rows[-1].domain_tag, 0) + 1
    return NegativesReport(
        rows=tuple(rows),
        by_subtype=by_subtype,
        by_domain=by_domain,
        annotator=annotator,
        warnings=tuple(_check_protocol(rows, by_subtype)),
    )


def _check_protocol(rows: Sequence[TrainingRow], by_subtype: dict[str, int]) -> list[str]:
    """Проверяет требования протокола; нарушения возвращаются предупреждениями, а не отказом."""
    warnings: list[str] = []
    total = len(rows)
    if total < MIN_NEGATIVES:
        warnings.append(f"негативов {total}, протокол требует не менее {MIN_NEGATIVES}")
    for subtype, target in TARGET_SHARES.items():
        share = by_subtype.get(subtype, 0) / max(total, 1)
        if abs(share - target) > SHARE_TOLERANCE:
            warnings.append(
                f"доля подтипа {subtype} = {share:.0%} при целевой {target:.0%} "
                f"(допуск ±{SHARE_TOLERANCE:.0%})"
            )
    if not any(row.annotation_agreement for row in rows):
        warnings.append(
            "перекрёстная разметка не выполнена: annotation_agreement пуст у всех строк, "
            "каппа Коэна не вычислена"
        )
    without_sources = sum(1 for row in rows if not row.source_urls)
    if without_sources:
        warnings.append(f"строк без источников: {without_sources} (протокол требует ≥ 1 ссылки)")
    return warnings


def write_annotations_template(rows: Sequence[TrainingRow], path: Path, source: Path) -> int:
    """Пишет CSV-лист разметки (`row_id, subtype, rationale`) для второго аннотатора."""
    payload = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    items = payload.get("items") or []
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row_id", "title", "subtype", "rationale"])
        for row, item in zip(rows, items, strict=True):
            writer.writerow([row.row_id, row.title, item.get("subtype", ""), item.get("rationale", "")])
    return len(rows)


def cohen_kappa(first: Sequence[int], second: Sequence[int]) -> float:
    """Каппа Коэна по совпадению меток двух аннотаторов (без зависимости от scikit-learn)."""
    if len(first) != len(second) or not first:
        raise NegativesError("списки разметки разной длины или пусты")
    total = len(first)
    observed = sum(1 for left, right in zip(first, second, strict=True) if left == right) / total
    labels = set(first) | set(second)
    expected = sum(
        (sum(1 for value in first if value == label) / total)
        * (sum(1 for value in second if value == label) / total)
        for label in labels
    )
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def read_second_annotation(path: Path) -> dict[str, int]:
    """Читает разметку второго аннотатора: CSV с колонками `row_id,label`."""
    result: dict[str, int] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            if record.get("row_id") and record.get("label", "").strip().isdigit():
                result[record["row_id"]] = int(record["label"])
    return result


def agreement_report(rows: Sequence[TrainingRow], second: dict[str, int]) -> dict[str, Any]:
    """Сводка согласия двух аннотаторов по общим строкам."""
    shared = [row for row in rows if row.row_id in second]
    if not shared:
        return {"rows_compared": 0, "kappa": None, "disagreements": []}
    first_labels = [row.label for row in shared]
    second_labels = [second[row.row_id] for row in shared]
    return {
        "rows_compared": len(shared),
        "kappa": cohen_kappa(first_labels, second_labels),
        "disagreements": [
            row.row_id
            for row, other in zip(shared, second_labels, strict=True)
            if row.label != other
        ],
    }
