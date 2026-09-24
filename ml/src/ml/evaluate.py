"""Сценарий `evaluate`: скоринг произвольного файла в формате исходного датасета (§7.4 HANDOFF)."""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from analyzer.domain.rules import RuleThresholds, apply_exclusion_rules, decide_by_score

from ml.dataset import DOMAIN_MAP, TrainingRow, clean_description, slugify
from ml.features import FeatureContext, build_matrix, row_features

TITLE_COLUMNS = ("Технология (слабый сигнал)", "tech", "title", "Технология")
DESCRIPTION_COLUMNS = ("Почему это слабый сигнал", "why", "description", "Описание")
DOMAIN_COLUMNS = ("Область", "domain", "domain_tag")
LABEL_COLUMNS = ("label", "Метка", "y")
PASSAGE_PREFIX = "passage: "


class EvaluateError(RuntimeError):
    """Файл для оценки имеет неизвестный формат."""


@dataclass(frozen=True, slots=True)
class ScoredRow:
    """Результат скоринга одной строки файла."""

    title: str
    score: float
    decision: str
    reason: str
    label: int | None


def read_table(path: Path) -> list[dict[str, str]]:
    """Читает csv/xlsx в список словарей по заголовкам первой непустой строки."""
    path = Path(path)
    if path.suffix.lower() in {".csv", ".tsv"}:
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(record) for record in csv.DictReader(handle, delimiter=delimiter)]
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook  # noqa: PLC0415 - необязательная зависимость
        except ImportError as error:
            raise EvaluateError("для чтения xlsx нужен пакет openpyxl") from error
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook[workbook.sheetnames[0]]
        rows = [[cell if cell is not None else "" for cell in row] for row in sheet.iter_rows(values_only=True)]
        header_index = next(
            (index for index, row in enumerate(rows) if sum(1 for cell in row if str(cell).strip()) >= 3),
            0,
        )
        header = [str(cell).strip() for cell in rows[header_index]]
        return [
            {key: str(value) for key, value in zip(header, row, strict=False) if key}
            for row in rows[header_index + 1 :]
            if any(str(cell).strip() for cell in row)
        ]
    raise EvaluateError(f"неизвестный формат файла: {path.suffix}; ожидаются csv, tsv, xlsx")


def rows_from_table(
    records: Sequence[dict[str, str]], patterns: Sequence[re.Pattern[str]] = ()
) -> list[TrainingRow]:
    """Преобразует записи файла в строки конвейера; отсутствующие колонки — понятная ошибка.

    Описание проходит ту же очистку `clean_description`, что и при сборке обучающего набора:
    иначе признаки оцениваемого файла считаются по другому тексту, чем признаки обучения.
    """
    if not records:
        raise EvaluateError("файл не содержит строк данных")
    title_key = _column(records[0], TITLE_COLUMNS)
    if title_key is None:
        raise EvaluateError(
            "не найдена колонка с названием технологии; ожидается одна из: "
            + ", ".join(TITLE_COLUMNS)
        )
    description_key = _column(records[0], DESCRIPTION_COLUMNS)
    domain_key = _column(records[0], DOMAIN_COLUMNS)
    label_key = _column(records[0], LABEL_COLUMNS)
    rows: list[TrainingRow] = []
    for index, record in enumerate(records, start=1):
        title = str(record.get(title_key, "")).strip()
        if not title:
            continue
        domain_raw = str(record.get(domain_key, "")).strip().lower() if domain_key else ""
        label_raw = str(record.get(label_key, "")).strip() if label_key else ""
        rows.append(
            TrainingRow(
                row_id=f"pos-{index:04d}",
                group_id=slugify(title),
                origin="gpb_dataset_2026_09",
                title=title,
                description=(
                    clean_description(str(record.get(description_key, "")).strip(), patterns)
                    if description_key
                    else ""
                ),
                domain_tag=DOMAIN_MAP.get(domain_raw, "other"),
                label=int(label_raw) if label_raw.isdigit() else 1,
                label_kind="WEAK_SIGNAL",
                source_urls=[],
                annotators=["methodologists_gpb"],
            )
        )
    return rows


def _column(record: dict[str, str], candidates: Sequence[str]) -> str | None:
    """Первая подходящая колонка из списка кандидатов."""
    lowered = {key.strip().lower(): key for key in record}
    for candidate in candidates:
        key = lowered.get(candidate.strip().lower())
        if key is not None:
            return key
    return None


def score_rows(
    rows: Sequence[TrainingRow],
    context: FeatureContext,
    embedder: Any,
    bundle: Any,
    thresholds: RuleThresholds,
) -> list[ScoredRow]:
    """Скорит строки локально тем же кодом, что и analyzer: правила имеют приоритет над моделью."""
    vectors = embedder.encode([row.text for row in rows], PASSAGE_PREFIX)
    results: list[ScoredRow] = []
    values_list = [
        row_features(row, context, vectors[index], bundle.weak_centroid, bundle.mature_centroid, None)
        for index, row in enumerate(rows)
    ]
    matrix = build_matrix(values_list, context.registry)
    probabilities = np.asarray(bundle.classifier.predict_proba(matrix), dtype=np.float64)
    for index, row in enumerate(rows):
        values = values_list[index]
        rule = apply_exclusion_rules(values, [], thresholds)
        score = float(probabilities[index])
        if rule is not None:
            decision, reason = rule.decision.value, rule.reason.value
        else:
            decision = decide_by_score(score, bundle.threshold, values, thresholds).value
            reason = "MODEL_SCORE"
        results.append(
            ScoredRow(
                title=row.title,
                score=score,
                decision=decision,
                reason=reason,
                label=row.label,
            )
        )
    return results


def summarize(scored: Sequence[ScoredRow], threshold: float, has_labels: bool) -> dict[str, Any]:
    """Сводка: метрики при наличии меток, иначе доли решений (§7.4 HANDOFF)."""
    total = len(scored)
    weak = sum(1 for item in scored if item.decision == "WEAK_SIGNAL")
    excluded_by_rules = sum(1 for item in scored if item.reason != "MODEL_SCORE")
    summary: dict[str, Any] = {
        "rows": total,
        "threshold": threshold,
        "weak_signals": weak,
        "weak_signal_share": weak / total if total else 0.0,
        "excluded_by_rules": excluded_by_rules,
        "excluded_by_rules_share": excluded_by_rules / total if total else 0.0,
        "confident_share": sum(1 for item in scored if item.score >= 0.75) / total if total else 0.0,
    }
    if has_labels and any(item.label is not None for item in scored):
        labels = np.asarray([item.label for item in scored])
        predicted = np.asarray([1 if item.decision == "WEAK_SIGNAL" else 0 for item in scored])
        if len(set(labels.tolist())) > 1:
            from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score  # noqa: PLC0415

            summary["accuracy"] = float(accuracy_score(labels, predicted))
            summary["precision"] = float(precision_score(labels, predicted, zero_division=0))
            summary["recall"] = float(recall_score(labels, predicted, zero_division=0))
            summary["f1"] = float(f1_score(labels, predicted, zero_division=0))
        else:
            summary["recall"] = float((predicted == labels).mean())
    return summary


def write_scores(path: Path, scored: Sequence[ScoredRow]) -> Path:
    """Сохраняет таблицу скорингов в CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["title", "score", "decision", "reason", "label"])
        for item in scored:
            writer.writerow(
                [item.title, f"{item.score:.4f}", item.decision, item.reason, item.label]
            )
    return path
