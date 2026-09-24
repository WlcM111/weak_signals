"""Экспорт контрольной точки в артефакт analyzer v2 (JSON без pickle) и явная активация с резервной копией."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from analyzer.domain.features_v2 import FEATURE_NAMES_V2, PASSAGE_PREFIX, QUERY_PREFIX, registry_payload
from analyzer.domain.rules import RuleThresholds

from ml.seq.stages import load_checkpoint

V2_FAMILY = "logreg_seq_laplace"
DATASET_VERSION = "ds-2026.09.24-v3"
ACTIVE_DIR = "active"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def query_similarity_floor(b_path: Path) -> float:
    """Нижняя граница правила OFF_TOPIC: 5-й перцентиль близости позитивов dev минус 0,02 (шкала e5 прогона)."""
    values = []
    for line in Path(b_path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("label") != 1 or not str(row.get("split", "")).startswith("dev_fold_"):
            continue
        observed = row.get("observed_runtime") or {}
        value = observed.get("emb_sim_query")
        if value is None:
            value = (observed.get("features_by_name", {}).get("emb_sim_query") or [None])[0]
        if value is not None:
            values.append(float(value))
    if not values:
        return RuleThresholds().min_query_similarity
    return float(np.floor((np.percentile(values, 5) - 0.02) * 100) / 100)


def build_payloads(checkpoint_path: Path, floor: float, version_id: str) -> tuple[dict, dict, dict]:
    """model_v2.json, rules.json и манифест (без sha256 файлов)."""
    ck, sha = load_checkpoint(checkpoint_path)
    if not 0.0 < ck.threshold < 1.0:
        raise ValueError("порог контрольной точки должен лежать в (0, 1); сначала выполните калибровку")
    weights, intercept = ck.transform.fold(ck.theta)
    lineage = {"checkpoint_id": ck.checkpoint_id, "checkpoint_sha256": sha, "stage": ck.stage,
               "variant": ck.variant, "parent_id": ck.parent_id, "parent_sha256": ck.parent_sha256,
               "params": {k: v for k, v in ck.params.items() if k != "replay_ids"},
               "replay_ids_count": len(ck.params.get("replay_ids", [])), "data": {
                   k: v for k, v in ck.data.items() if not k.endswith("_ids")}}
    model = {"format": "wsclf-v2", "feature_names": list(FEATURE_NAMES_V2), "feature_registry": registry_payload(),
             "weights": weights.tolist(), "intercept": intercept, "calibration": ck.calibration,
             "threshold": ck.threshold, "projection": ck.projection.to_json(), "glossary": ck.glossary,
             "query_prefix": QUERY_PREFIX, "passage_prefix": PASSAGE_PREFIX, "embedding_model": ck.embedding_model,
             "lineage": lineage}
    rules = {"version": 2, "thresholds": {**asdict(RuleThresholds()), "min_query_similarity": floor},
             "feature_defaults": {}}
    holdout = (ck.metrics or {}).get("holdout") or (ck.metrics or {}).get("dev_oof") or {}
    at = holdout.get("at_dev_threshold") or {}
    manifest = {
        "model_version_id": version_id, "model_family": V2_FAMILY, "feature_schema_version": "v2",
        "embedding_model": ck.embedding_model, "dataset_version": DATASET_VERSION, "threshold": ck.threshold,
        "metrics": {
            "test": {"accuracy": float(at.get("accuracy", 0.0)), "precision": float(at.get("precision", 0.0)),
                     "recall": float(at.get("recall", 0.0)), "f1": float(at.get("f1", 0.0)),
                     "roc_auc": float(holdout.get("roc_auc") or 0.0), "n": int(holdout.get("n", 0)),
                     "pr_auc": holdout.get("pr_auc"), "source": "holdout" if ck.metrics.get("holdout") else "dev_oof"},
            "cv": {"protocol": "B: 5 тематических dev-фолдов + фиксированный holdout тем; A: групповой split",
                   "f1_mean": float(((ck.metrics or {}).get("dev_oof") or {}).get("pr_auc") or 0.0),
                   "f1_std": 0.0, "accuracy_mean": 0.0, "accuracy_std": 0.0},
        },
        "trained_at": ck.created_at, "git_commit": "", "stage_model_present": False, "trend_model_present": False,
        "lineage": lineage,
    }
    return model, rules, manifest


def export(checkpoint_path: Path, store_dir: Path, version_id: str, floor: float, activate: bool) -> Path:
    """Пишет версию (существующая не перезаписывается); при activate копирует в active с резервом прежней."""
    model, rules, manifest = build_payloads(checkpoint_path, floor, version_id)
    store_dir = Path(store_dir)
    target = store_dir / version_id
    if target.exists():
        raise FileExistsError(f"версия {version_id} уже есть в {store_dir}: выберите новый номер")
    staging = store_dir / f".{version_id}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name, payload in (("model_v2.json", model), ("rules.json", rules)):
        (staging / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    manifest["artifact_files"] = {"model": "model_v2.json",
                                  "sha256": {"model": _sha(staging / "model_v2.json"),
                                             "rules.json": _sha(staging / "rules.json")}}
    (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    staging.rename(target)
    if activate:
        activate_version(store_dir, version_id)
    return target


def activate_version(store_dir: Path, version_id: str) -> Path | None:
    """Делает версию активной; прежний active сохраняется в rollback/active-<время>. Возвращает путь резерва."""
    store_dir = Path(store_dir)
    target = store_dir / version_id
    if not (target / "manifest.json").is_file():
        raise FileNotFoundError(f"нет версии {version_id} в {store_dir}")
    active = store_dir / ACTIVE_DIR
    backup = None
    if active.exists():
        backup = store_dir / "rollback" / f"active-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(active, backup)
        shutil.rmtree(active)
    shutil.copytree(target, active)
    return backup


def describe(path: Path) -> dict[str, Any]:
    """Краткое описание артефакта (для проверки после экспорта)."""
    manifest = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
    return {k: manifest[k] for k in ("model_version_id", "model_family", "feature_schema_version", "embedding_model",
                                     "threshold")} | {"lineage": manifest.get("lineage", {}).get("checkpoint_id")}
