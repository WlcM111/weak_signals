"""Конфигурация ML-конвейера: переменные `WS_*` и пути по умолчанию (§12 HANDOFF_ML_PIPELINE)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED = 20260915


def _path(name: str, default: Path) -> Path:
    """Путь из переменной окружения или значение по умолчанию."""
    value = os.getenv(name)
    return Path(value) if value else default


def _int(name: str, default: int) -> int:
    """Целое из переменной окружения."""
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    """Дробное из переменной окружения."""
    raw = os.getenv(name)
    return float(raw) if raw else default


@dataclass(frozen=True, slots=True)
class MlSettings:
    """Настройки конвейера; читаются один раз в `cli.py`."""

    data_dir: Path = field(default_factory=lambda: _path("WS_DATA_DIR", REPO_ROOT / "ml" / "data"))
    model_store_dir: Path = field(
        default_factory=lambda: _path("WS_MODEL_STORE_DIR", REPO_ROOT / "model-store")
    )
    reports_dir: Path = field(default_factory=lambda: _path("WS_REPORTS_DIR", REPO_ROOT / "ml" / "reports"))
    docs_dir: Path = field(default_factory=lambda: _path("WS_DOCS_DIR", REPO_ROOT / "docs" / "ml"))
    feature_registry_path: Path = field(
        default_factory=lambda: _path(
            "WS_FEATURE_REGISTRY_PATH", REPO_ROOT / "schemas" / "feature_registry_v1.json"
        )
    )
    training_row_schema_path: Path = field(
        default_factory=lambda: REPO_ROOT / "schemas" / "training_row.schema.json"
    )
    lexicon_dir: Path = field(
        default_factory=lambda: _path(
            "WS_LEXICON_DIR", REPO_ROOT / "services" / "analyzer" / "config" / "lexicons"
        )
    )
    stage_rules_path: Path = field(
        default_factory=lambda: _path(
            "WS_STAGE_RULES_PATH", REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml"
        )
    )
    leak_patterns_path: Path = field(default_factory=lambda: REPO_ROOT / "ml" / "leak_patterns.yaml")
    collector_addr: str = field(default_factory=lambda: os.getenv("WS_COLLECTOR_ADDR", "collector:50051"))
    mlflow_tracking_uri: str = field(
        default_factory=lambda: os.getenv("WS_MLFLOW_TRACKING_URI", os.getenv("MLFLOW_TRACKING_URI", ""))
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("WS_EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    )
    hf_home: Path = field(default_factory=lambda: _path("HF_HOME", REPO_ROOT / ".hf-cache"))
    seed: int = field(default_factory=lambda: _int("WS_TRAIN_SEED", DEFAULT_SEED))
    holdout_share: float = field(default_factory=lambda: _float("WS_TRAIN_HOLDOUT_SHARE", 0.2))
    cv_repeats: int = field(default_factory=lambda: _int("WS_TRAIN_CV_REPEATS", 5))
    cv_folds: int = field(default_factory=lambda: _int("WS_TRAIN_CV_FOLDS", 5))
    min_precision: float = field(default_factory=lambda: _float("WS_TRAIN_MIN_PRECISION", 0.80))
    enrichment_timeout_seconds: float = field(
        default_factory=lambda: _float("WS_ENRICHMENT_TIMEOUT_SECONDS", 90.0)
    )
    max_positive_exclusion_share: float = field(
        default_factory=lambda: _float("WS_TRAIN_MAX_RULE_EXCLUSION", 0.05)
    )

    @property
    def labels_dir(self) -> Path:
        """Каталог размеченных наборов."""
        return self.data_dir / "labels"

    @property
    def raw_dir(self) -> Path:
        """Каталог исходных данных организаторов."""
        return self.data_dir / "raw"

    @property
    def enriched_dir(self) -> Path:
        """Каталог ENRICHMENT-коллекций."""
        return self.data_dir / "enriched"

    @property
    def features_dir(self) -> Path:
        """Каталог таблиц признаков."""
        return self.data_dir / "features"
