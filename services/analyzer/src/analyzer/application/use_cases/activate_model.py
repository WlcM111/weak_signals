"""Сценарий ActivateModelFromStore: загрузка и регистрация активной модели (§7 HANDOFF)."""

from __future__ import annotations

from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import ModelBundle
from analyzer.application.ports import Embedder, MetricsSink, ModelStore, ModelVersionRepository, NullMetrics
from analyzer.domain.errors import InvariantViolation
from analyzer.domain.feature_registry import FeatureRegistry
from ws_common.logging import get_logger


class ActivateModelFromStore:
    """Читает манифест из `model-store`, проверяет совместимость и делает версию активной."""

    def __init__(
        self,
        store: ModelStore,
        registry: FeatureRegistry,
        versions: ModelVersionRepository,
        holder: ActiveModelHolder,
        embedder: Embedder | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._versions = versions
        self._holder = holder
        self._embedder = embedder
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("analyzer.model")

    def execute(self, path: str | None = None) -> ModelBundle:
        """Загружает активную модель (или конкретную версию) и регистрирует её в реестре версий."""
        bundle = (
            self._store.load_active(self._registry)
            if path is None
            else self._store.load_from_path(path, self._registry)
        )
        self._check_compatibility(bundle)
        self._versions.activate(bundle.version)
        self._holder.set(bundle)
        self._metrics.model_activated(bundle.version.model_version_id)
        self._log.info(
            "model.loaded",
            version=bundle.version.model_version_id,
            family=bundle.version.model_family,
            sha256=bundle.version.artifact_sha256,
            threshold=bundle.threshold,
            feature_schema_version=bundle.version.feature_schema_version,
            embedding_model=bundle.version.embedding_model,
        )
        return bundle

    def _check_compatibility(self, bundle: ModelBundle) -> None:
        """Совпадение версии реестра признаков, состава признаков и модели эмбеддингов."""
        registry_version = bundle.registry.version if bundle.feature_schema == "v2" else self._registry.version
        if bundle.version.feature_schema_version != registry_version:
            raise InvariantViolation(
                f"модель обучена на реестре признаков {bundle.version.feature_schema_version}, "
                f"сервис использует {self._registry.version}"
            )
        expected = bundle.registry.model_names if bundle.feature_schema == "v2" else self._registry.model_names
        actual = tuple(bundle.classifier.feature_names)
        if actual and actual != expected:
            raise InvariantViolation(
                "состав или порядок признаков модели не совпадает с реестром: "
                f"{len(actual)} против {len(expected)}"
            )
        if self._embedder is not None and bundle.version.embedding_model != self._embedder.model_name:
            raise InvariantViolation(
                f"модель обучена с эмбеддером {bundle.version.embedding_model}, "
                f"загружен {self._embedder.model_name}"
            )
