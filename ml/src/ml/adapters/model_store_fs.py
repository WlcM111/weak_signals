"""Запись артефактов модели в `model-store` с манифестом и атомарным обновлением `active`."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from ws_common.logging import get_logger

ACTIVE_DIR = "active"


class FileSystemModelStoreWriter:
    """Реализация порта `ModelStore`: каталог версии + копия в `active`."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._log = get_logger("ml.export")

    def export(self, version_id: str, artifacts: dict[str, Any], manifest: dict[str, Any]) -> Path:
        """Пишет артефакты во временный каталог, считает sha256, затем переносит в версию и `active`."""
        self._root.mkdir(parents=True, exist_ok=True)
        staging = self._root / f".{version_id}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        joblib.dump(artifacts["classifier"], staging / "classifier.joblib")
        joblib.dump(artifacts["scaler"], staging / "scaler.joblib")
        joblib.dump(artifacts["calibrator"], staging / "calibrator.joblib")
        np.savez(
            staging / "centroids.npz",
            weak=np.asarray(artifacts["weak_centroid"], dtype=np.float32),
            mature=np.asarray(artifacts["mature_centroid"], dtype=np.float32),
        )
        _write_json(staging / "rules.json", artifacts["rules"])
        _write_json(staging / "feature_defaults.json", artifacts["rules"]["feature_defaults"])
        for name in ("stage_model", "trend_model"):
            if artifacts.get(name) is not None:
                joblib.dump(artifacts[name], staging / f"{name}.joblib")

        files = {
            "classifier": "classifier.joblib",
            "scaler": "scaler.joblib",
            "calibrator": "calibrator.joblib",
            "centroids": "centroids.npz",
        }
        for name in ("stage_model", "trend_model"):
            if (staging / f"{name}.joblib").exists():
                files[name] = f"{name}.joblib"
        checksums = {key: _sha256(staging / path) for key, path in files.items()}
        for extra in ("rules.json", "feature_defaults.json"):
            checksums[extra] = _sha256(staging / extra)
        manifest = {**manifest, "artifact_files": {**files, "sha256": checksums}}
        _write_json(staging / "manifest.json", manifest)

        target = self._root / version_id
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        active = self._root / ACTIVE_DIR
        if active.exists():
            shutil.rmtree(active)
        shutil.copytree(target, active)
        self._log.info(
            "ml.export",
            version=version_id,
            sha256=_sha256(target / "manifest.json"),
            files=len(checksums),
            directory=str(target),
        )
        return target


def _write_json(path: Path, payload: Any) -> None:
    """Пишет JSON с отступами и кириллицей как есть."""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    """Контрольная сумма файла."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
