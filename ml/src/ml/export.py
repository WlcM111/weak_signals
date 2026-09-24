"""Сценарий `export`: калибровка порогов правил, манифест и запись артефакта (§7.3 HANDOFF)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyzer.domain.rules import RuleThresholds, apply_exclusion_rules

from ws_common.logging import get_logger

RELAXATION_STEPS = 40
log = get_logger("ml.export")


def calibrate_rule_thresholds(
    positive_values: Sequence[dict[str, float]],
    max_exclusion_share: float,
    start: RuleThresholds | None = None,
) -> tuple[RuleThresholds, float, dict[str, int]]:
    """Ослабляет пороги правил, пока они исключают больше `max_exclusion_share` позитивов.

    Правила 1–3 на обучающих строках неприменимы (нет запроса и коллекции документов), поэтому
    измеряется вклад правил 4–8 — ровно то, что требует §12.6 ТЗ («исключать ≤ 5 % позитивов»).
    """
    thresholds = start or RuleThresholds()
    share, reasons = _exclusion_share(positive_values, thresholds)
    for _ in range(RELAXATION_STEPS):
        if share <= max_exclusion_share:
            break
        thresholds = _relax(thresholds, reasons)
        share, reasons = _exclusion_share(positive_values, thresholds)
    log.info(
        "ml.rules.calibrated",
        excluded_share=round(share, 4),
        maturity_min=thresholds.maturity_min,
        hype_min=thresholds.hype_min,
        share_marketing_min=thresholds.share_marketing_min,
    )
    return thresholds, share, reasons


def _exclusion_share(
    values: Sequence[dict[str, float]], thresholds: RuleThresholds
) -> tuple[float, dict[str, int]]:
    """Доля позитивов, исключённых правилами, и распределение по причинам."""
    reasons: dict[str, int] = {}
    excluded = 0
    for row in values:
        result = apply_exclusion_rules(row, [], thresholds)
        if result is not None:
            excluded += 1
            reasons[result.reason.value] = reasons.get(result.reason.value, 0) + 1
    return (excluded / len(values) if values else 0.0), reasons


def _relax(thresholds: RuleThresholds, reasons: dict[str, int]) -> RuleThresholds:
    """Поднимает порог того правила, которое чаще других исключает позитивы."""
    if not reasons:
        return thresholds
    worst = max(reasons.items(), key=lambda item: item[1])[0]
    mapping = {
        "MATURITY_LEXICON": ("maturity_min", 0.05),
        "HYPE_LEXICON": ("hype_min", 0.05),
        "MARKETING_DOMINANT": ("share_marketing_min", 0.05),
        "MARKET_LEADERS": ("bigtech_min", 1.0),
        "ENCYCLOPEDIA_MATURE": ("wiki_pageviews_min", 5000.0),
    }
    field, step = mapping.get(worst, ("maturity_min", 0.05))
    current = getattr(thresholds, field)
    return replace(thresholds, **{field: min(current + step, 1.0 if step < 1 else current + step)})


def build_manifest(
    version_id: str,
    dataset_version: str,
    embedding_model: str,
    threshold: float,
    test_metrics: dict[str, Any],
    cv_metrics: dict[str, float],
    family: str = "logreg_elasticnet",
    stage_model_present: bool = False,
    trend_model_present: bool = False,
) -> dict[str, Any]:
    """Манифест модели по `model_manifest.schema.json` (без `artifact_files` — их считает хранилище)."""
    return {
        "model_version_id": version_id,
        "model_family": family,
        "feature_schema_version": "v1",
        "embedding_model": embedding_model,
        "dataset_version": dataset_version,
        "threshold": round(float(threshold), 4),
        "metrics": {
            "test": {
                "accuracy": round(float(test_metrics["accuracy"]), 4),
                "precision": round(float(test_metrics["precision"]), 4),
                "recall": round(float(test_metrics["recall"]), 4),
                "f1": round(float(test_metrics["f1"]), 4),
                "roc_auc": round(float(test_metrics["roc_auc"]), 4),
                "n": int(test_metrics["n"]),
            },
            "cv": {
                "protocol": "holdout_test_20pct + repeated_stratified_5x5cv",
                "f1_mean": round(float(cv_metrics["f1_mean"]), 4),
                "f1_std": round(float(cv_metrics["f1_std"]), 4),
                "accuracy_mean": round(float(cv_metrics["accuracy_mean"]), 4),
                "accuracy_std": round(float(cv_metrics["accuracy_std"]), 4),
            },
        },
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": current_commit(),
        "stage_model_present": stage_model_present,
        "trend_model_present": trend_model_present,
    }


def build_rules_payload(thresholds: RuleThresholds, feature_defaults: dict[str, float]) -> dict[str, Any]:
    """Содержимое `rules.json`: пороги правил и значения-заглушки признаков для ScoreText."""
    return {
        "version": 1,
        "thresholds": {key: float(value) for key, value in asdict(thresholds).items()},
        "feature_defaults": {key: float(value) for key, value in sorted(feature_defaults.items())},
    }


def next_version_id(model_store: Path, today: datetime | None = None) -> str:
    """Следующий идентификатор версии `wsclf-YYYY.MM.DD-N` для текущей даты."""
    moment = today or datetime.now(UTC)
    prefix = f"wsclf-{moment:%Y.%m.%d}-"
    existing = [path.name for path in Path(model_store).glob(f"{prefix}*")] if Path(model_store).is_dir() else []
    numbers = [int(name.rsplit("-", 1)[-1]) for name in existing if name.rsplit("-", 1)[-1].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


def dataset_version_for(today: datetime | None = None, number: int = 1) -> str:
    """Идентификатор версии датасета `ds-YYYY.MM.DD-vN`."""
    moment = today or datetime.now(UTC)
    return f"ds-{moment:%Y.%m.%d}-v{number}"


def current_commit() -> str:
    """Хеш текущего коммита; пустая строка, если репозиторий недоступен."""
    try:
        result = subprocess.run(  # noqa: S603, S607 - фиксированная команда без пользовательского ввода
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    commit = result.stdout.strip()
    return commit if len(commit) == 40 else ""
