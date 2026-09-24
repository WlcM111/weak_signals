"""Загрузка артефактов модели из volume `model-store` с проверкой sha256 (§17 HANDOFF).

joblib исполняет код при загрузке, поэтому артефакт принимается только из каталога хранилища
и только после сверки контрольных сумм из манифеста.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from analyzer.adapters.outbound.sklearn_model import SklearnClassifier
from analyzer.adapters.outbound.v2_model import V2_FAMILY, LinearV2Classifier, V2Spec
from analyzer.domain.features_v2 import Projection
from analyzer.application.dto import ModelBundle
from analyzer.domain.entities import ModelMetrics, ModelVersion
from analyzer.domain.errors import InvariantViolation
from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.rules import RuleThresholds
from ws_common.logging import get_logger

MANIFEST_NAME = "manifest.json"
RULES_NAME = "rules.json"
ACTIVE_DIR = "active"
REQUIRED_ARTIFACTS = ("classifier", "scaler", "calibrator", "centroids")
_VERSION_RE = re.compile(r"^wsclf-[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]+$")
_DATASET_RE = re.compile(r"^ds-[0-9]{4}\.[0-9]{2}\.[0-9]{2}-v[0-9]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ModelStoreError(RuntimeError):
    """Артефакт модели отсутствует, повреждён или не прошёл проверку контрольной суммы."""


class FileSystemModelStore:
    """Реализация порта `ModelStore` поверх каталога `model-store`."""

    def __init__(self, root: Path, require_sha256: bool = True) -> None:
        self._root = Path(root).resolve()
        self._require_sha256 = require_sha256
        self._log = get_logger("analyzer.model_store")

    def load_active(self, registry: FeatureRegistry) -> ModelBundle:
        """Загружает версию из `<model-store>/active`."""
        return self._load(self._root / ACTIVE_DIR, registry)

    def load_from_path(self, path: str, registry: FeatureRegistry) -> ModelBundle:
        """Загружает конкретную версию; путь обязан находиться внутри `model-store`."""
        directory = Path(path)
        if not directory.is_absolute():
            directory = self._root / directory
        return self._load(directory, registry)

    def _load(self, directory: Path, registry: FeatureRegistry) -> ModelBundle:
        """Читает манифест, проверяет суммы и собирает модель."""
        directory = self._safe_directory(directory)
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file():
            raise ModelStoreError(f"манифест не найден: {manifest_path}")
        manifest = _read_json(manifest_path)
        if str(manifest.get("feature_schema_version")) == "v2":
            return self._load_v2(directory, manifest, manifest_path)
        _validate_manifest(manifest)
        checksums = manifest["artifact_files"].get("sha256", {})
        self._verify_checksums(directory, manifest["artifact_files"], checksums)
        artifacts = {
            name: joblib.load(self._artifact_path(directory, manifest["artifact_files"][name]))
            for name in ("classifier", "scaler", "calibrator")
        }
        weak, mature = _load_centroids(
            self._artifact_path(directory, manifest["artifact_files"]["centroids"])
        )
        rules_payload = _read_json(directory / RULES_NAME) if (directory / RULES_NAME).is_file() else {}
        classifier = SklearnClassifier(
            scaler=artifacts["scaler"],
            classifier=artifacts["classifier"],
            calibrator=artifacts["calibrator"],
            feature_names=registry.model_names,
            model_family=manifest["model_family"],
            stage_model=self._optional_artifact(directory, manifest, "stage_model"),
            trend_model=self._optional_artifact(directory, manifest, "trend_model"),
        )
        version = _build_version(manifest, directory.relative_to(self._root).as_posix(), manifest_path)
        self._log.info(
            "model.artifacts_verified",
            version=version.model_version_id,
            files=len(checksums),
            directory=str(directory),
        )
        return ModelBundle(
            version=version,
            classifier=classifier,
            registry=registry,
            thresholds=RuleThresholds.from_mapping(rules_payload.get("thresholds")),
            weak_centroid=weak,
            mature_centroid=mature,
            feature_defaults={
                str(name): float(value)
                for name, value in (rules_payload.get("feature_defaults") or {}).items()
            },
        )

    def _load_v2(self, directory: Path, manifest: dict[str, Any], manifest_path: Path) -> ModelBundle:
        """Артефакт v2: JSON-модель без pickle, собственный реестр признаков и проекция Stage A."""
        _validate_manifest_v2(manifest)
        files = manifest["artifact_files"]
        checksums = files.get("sha256", {})
        for key in ("model", RULES_NAME):
            if key not in checksums:
                raise ModelStoreError(f"в манифесте v2 нет sha256 для {key}")
        for key, expected in checksums.items():
            if not _SHA256_RE.match(str(expected)):
                raise ModelStoreError(f"sha256 для {key} имеет неверный формат")
            actual = _sha256_of(self._artifact_path(directory, str(files.get(key, key))))
            if actual != expected:
                raise ModelStoreError(f"контрольная сумма артефакта {key} не совпала: ожидалась {expected}, получена {actual}")
        payload = _read_json(self._artifact_path(directory, str(files["model"])))
        try:
            registry = FeatureRegistry.from_mapping(payload["feature_registry"])
            names = tuple(payload["feature_names"])
            if registry.version != "v2" or names != registry.model_names:
                raise ModelStoreError("состав признаков модели v2 не совпадает с её реестром")
            classifier = LinearV2Classifier(
                names,
                payload["weights"],
                float(payload["intercept"]),
                (float(payload["calibration"]["slope"]), float(payload["calibration"]["offset"])),
            )
            spec = V2Spec(
                projection=Projection.from_json(payload["projection"]),
                glossary={str(k): str(v) for k, v in (payload.get("glossary") or {}).items()},
                query_prefix=str(payload.get("query_prefix", "query: ")),
                passage_prefix=str(payload.get("passage_prefix", "passage: ")),
                lineage=dict(payload.get("lineage") or {}),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ModelStoreError(f"артефакт v2 повреждён: {error}") from error
        if spec.projection.components.shape[0] != sum(1 for name in names if name.startswith("emb_pc_")):
            raise ModelStoreError("число компонент проекции не совпадает с признаками emb_pc_*")
        rules_payload = _read_json(directory / RULES_NAME)
        version = _build_version(manifest, directory.relative_to(self._root).as_posix(), manifest_path)
        self._log.info("model.artifacts_verified", version=version.model_version_id, files=len(checksums),
                       directory=str(directory), feature_schema="v2", parent=spec.lineage.get("parent_id", ""))
        return ModelBundle(
            version=version,
            classifier=classifier,
            registry=registry,
            thresholds=RuleThresholds.from_mapping(rules_payload.get("thresholds")),
            feature_schema="v2",
            v2=spec,
        )

    def _safe_directory(self, directory: Path) -> Path:
        """Каталог версии обязан находиться внутри `model-store` (защита от произвольных путей)."""
        resolved = directory.resolve()
        if not resolved.is_dir():
            raise ModelStoreError(f"каталог модели не найден: {resolved}")
        if self._root not in resolved.parents and resolved != self._root:
            raise ModelStoreError(f"путь {resolved} находится вне model-store {self._root}")
        return resolved

    def _artifact_path(self, directory: Path, relative: str) -> Path:
        """Абсолютный путь артефакта с проверкой выхода за пределы каталога версии."""
        path = (directory / relative).resolve()
        if directory not in path.parents:
            raise ModelStoreError(f"артефакт {relative} находится вне каталога версии")
        if not path.is_file():
            raise ModelStoreError(f"артефакт не найден: {path}")
        return path

    def _verify_checksums(
        self, directory: Path, artifact_files: dict[str, Any], checksums: dict[str, str]
    ) -> None:
        """Сверяет sha256 всех перечисленных файлов; расхождение — отказ загрузки."""
        for name in REQUIRED_ARTIFACTS:
            if name not in checksums:
                if self._require_sha256:
                    raise ModelStoreError(f"в манифесте нет sha256 для артефакта {name}")
                self._log.warning("model.sha256_missing", artifact=name)
        for key, expected in checksums.items():
            if not _SHA256_RE.match(str(expected)):
                raise ModelStoreError(f"sha256 для {key} имеет неверный формат")
            relative = artifact_files.get(key, key)
            path = self._artifact_path(directory, str(relative))
            actual = _sha256_of(path)
            if actual != expected:
                raise ModelStoreError(
                    f"контрольная сумма артефакта {relative} не совпала: ожидалась {expected}, "
                    f"получена {actual}"
                )

    def _optional_artifact(self, directory: Path, manifest: dict[str, Any], name: str) -> Any:
        """Загружает вспомогательную модель, если она объявлена и присутствует."""
        relative = manifest["artifact_files"].get(name)
        flag = manifest.get(f"{name}_present", False)
        if not relative or not flag:
            return None
        return joblib.load(self._artifact_path(directory, str(relative)))


def _read_json(path: Path) -> dict[str, Any]:
    """Читает JSON-файл артефакта."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelStoreError(f"не удалось прочитать {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ModelStoreError(f"{path}: ожидается объект JSON")
    return payload


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Проверяет обязательные поля манифеста (`model_manifest.schema.json`)."""
    required = (
        "model_version_id",
        "model_family",
        "feature_schema_version",
        "embedding_model",
        "dataset_version",
        "artifact_files",
        "threshold",
        "metrics",
        "trained_at",
    )
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ModelStoreError(f"в манифесте отсутствуют поля: {', '.join(missing)}")
    if not _VERSION_RE.match(str(manifest["model_version_id"])):
        raise ModelStoreError("model_version_id не соответствует шаблону wsclf-YYYY.MM.DD-N")
    if not _DATASET_RE.match(str(manifest["dataset_version"])):
        raise ModelStoreError("dataset_version не соответствует шаблону ds-YYYY.MM.DD-vN")
    if manifest["model_family"] not in {"logreg_elasticnet", "lightgbm"}:
        raise ModelStoreError("model_family должен быть logreg_elasticnet или lightgbm")
    if not 0.0 < float(manifest["threshold"]) < 1.0:
        raise ModelStoreError("threshold должен лежать строго между 0 и 1")
    files = manifest["artifact_files"]
    if not isinstance(files, dict) or any(name not in files for name in REQUIRED_ARTIFACTS):
        raise ModelStoreError("artifact_files должен содержать classifier, scaler, calibrator, centroids")


def _validate_manifest_v2(manifest: dict[str, Any]) -> None:
    """Обязательные поля манифеста v2 (`model_manifest.schema.json`, ветка v2)."""
    required = ("model_version_id", "model_family", "embedding_model", "dataset_version", "artifact_files",
                "threshold", "metrics", "trained_at")
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ModelStoreError(f"в манифесте отсутствуют поля: {', '.join(missing)}")
    if not _VERSION_RE.match(str(manifest["model_version_id"])):
        raise ModelStoreError("model_version_id не соответствует шаблону wsclf-YYYY.MM.DD-N")
    if not _DATASET_RE.match(str(manifest["dataset_version"])):
        raise ModelStoreError("dataset_version не соответствует шаблону ds-YYYY.MM.DD-vN")
    if manifest["model_family"] != V2_FAMILY:
        raise ModelStoreError(f"model_family для v2 должен быть {V2_FAMILY}")
    if not 0.0 < float(manifest["threshold"]) < 1.0:
        raise ModelStoreError("threshold должен лежать строго между 0 и 1")
    if "model" not in manifest["artifact_files"]:
        raise ModelStoreError("artifact_files v2 должен содержать model")


def _load_centroids(path: Path) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Читает эталонные центроиды `centroids.npz` (ключи `weak` и `mature`)."""
    try:
        with np.load(path) as payload:
            weak = payload["weak"] if "weak" in payload else None
            mature = payload["mature"] if "mature" in payload else None
    except (OSError, ValueError, KeyError) as error:
        raise ModelStoreError(f"не удалось прочитать центроиды {path}: {error}") from error
    return (
        np.asarray(weak, dtype=np.float32) if weak is not None else None,
        np.asarray(mature, dtype=np.float32) if mature is not None else None,
    )


def _build_version(manifest: dict[str, Any], artifact_path: str, manifest_path: Path) -> ModelVersion:
    """Собирает сущность версии модели; sha256 манифеста фиксирует состав всех артефактов."""
    test_metrics = manifest["metrics"]["test"]
    protocol = str(manifest["metrics"].get("cv", {}).get("protocol", "unspecified"))
    try:
        trained_at = datetime.fromisoformat(str(manifest["trained_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise ModelStoreError(f"trained_at не является датой ISO 8601: {error}") from error
    if trained_at.tzinfo is None:
        trained_at = trained_at.replace(tzinfo=UTC)
    return ModelVersion(
        model_version_id=str(manifest["model_version_id"]),
        model_family=str(manifest["model_family"]),
        feature_schema_version=str(manifest["feature_schema_version"]),
        embedding_model=str(manifest["embedding_model"]),
        dataset_version=str(manifest["dataset_version"]),
        artifact_path=artifact_path,
        artifact_sha256=_sha256_of(manifest_path),
        trained_at=trained_at,
        metrics=ModelMetrics(
            accuracy=float(test_metrics["accuracy"]),
            precision=float(test_metrics["precision"]),
            recall=float(test_metrics["recall"]),
            f1=float(test_metrics["f1"]),
            roc_auc=float(test_metrics["roc_auc"]),
            threshold=float(manifest["threshold"]),
            test_size=int(test_metrics["n"]),
            evaluation_protocol=protocol,
        ),
        git_commit=str(manifest.get("git_commit", "")),
        stage_model_present=bool(manifest.get("stage_model_present", False)),
        trend_model_present=bool(manifest.get("trend_model_present", False)),
    )


def _sha256_of(path: Path) -> str:
    """Контрольная сумма файла."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_registry_compatibility(registry: FeatureRegistry, manifest_version: str) -> None:
    """Совместимость версии реестра признаков (используется при регистрации модели)."""
    if registry.version != manifest_version:
        raise InvariantViolation(
            f"реестр признаков сервиса {registry.version} не совпадает с моделью {manifest_version}"
        )
