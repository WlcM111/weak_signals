"""Реестр версий модели (таблица `model_versions`, ровно одна активная)."""

from __future__ import annotations

from typing import Any

from analyzer.adapters.outbound.postgres.mappers import MODEL_VERSION_COLUMNS, row_to_model_version
from analyzer.domain.entities import ModelVersion

_DEACTIVATE_OTHERS = "UPDATE model_versions SET is_active = false WHERE is_active AND model_version_id <> %s"

_UPSERT = """
INSERT INTO model_versions (
  model_version_id, model_family, feature_schema_version, embedding_model, dataset_version,
  artifact_path, artifact_sha256, git_commit, trained_at, threshold, accuracy, precision_score,
  recall_score, f1_score, roc_auc, test_size, evaluation_protocol, stage_model_present,
  trend_model_present, is_active
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
ON CONFLICT (model_version_id) DO UPDATE SET
  model_family = EXCLUDED.model_family,
  feature_schema_version = EXCLUDED.feature_schema_version,
  embedding_model = EXCLUDED.embedding_model,
  dataset_version = EXCLUDED.dataset_version,
  artifact_path = EXCLUDED.artifact_path,
  artifact_sha256 = EXCLUDED.artifact_sha256,
  git_commit = EXCLUDED.git_commit,
  trained_at = EXCLUDED.trained_at,
  threshold = EXCLUDED.threshold,
  accuracy = EXCLUDED.accuracy,
  precision_score = EXCLUDED.precision_score,
  recall_score = EXCLUDED.recall_score,
  f1_score = EXCLUDED.f1_score,
  roc_auc = EXCLUDED.roc_auc,
  test_size = EXCLUDED.test_size,
  evaluation_protocol = EXCLUDED.evaluation_protocol,
  stage_model_present = EXCLUDED.stage_model_present,
  trend_model_present = EXCLUDED.trend_model_present,
  is_active = true
"""

_SELECT_ACTIVE = f"SELECT {MODEL_VERSION_COLUMNS} FROM model_versions WHERE is_active"


class PostgresModelVersionRepository:
    """Реализация порта `ModelVersionRepository`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def activate(self, version: ModelVersion) -> None:
        """Регистрирует версию и делает её единственной активной (частичный уникальный индекс)."""
        metrics = version.metrics
        with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(_DEACTIVATE_OTHERS, (version.model_version_id,))
            cur.execute(
                _UPSERT,
                (
                    version.model_version_id,
                    version.model_family,
                    version.feature_schema_version,
                    version.embedding_model,
                    version.dataset_version,
                    version.artifact_path,
                    version.artifact_sha256,
                    version.git_commit or None,
                    version.trained_at,
                    metrics.threshold,
                    metrics.accuracy,
                    metrics.precision,
                    metrics.recall,
                    metrics.f1,
                    metrics.roc_auc,
                    metrics.test_size,
                    metrics.evaluation_protocol,
                    version.stage_model_present,
                    version.trend_model_present,
                ),
            )

    def get_active(self) -> ModelVersion | None:
        """Активная версия модели."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_SELECT_ACTIVE)
            row = cur.fetchone()
        return row_to_model_version(dict(row)) if row else None
