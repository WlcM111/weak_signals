"""Преобразование строк PostgreSQL в доменные объекты analyzer."""

from __future__ import annotations

from typing import Any

from analyzer.domain.entities import Analysis, ModelMetrics, ModelVersion
from analyzer.domain.values import AnalysisParams, AnalysisStats, Lease, OperationStatus

def qualify(columns: str, alias: str) -> str:
    """Список колонок с псевдонимом таблицы: нужен, когда в запросе две таблицы с одноимёнными колонками."""
    return ", ".join(f"{alias}.{name.strip()}" for name in columns.split(","))


ANALYSIS_COLUMNS = (
    "analysis_id, idempotency_key, collection_id, query_text, model_version_id, top_n, "
    "max_candidates, weak_signal_threshold, min_evidence_documents, status, cancel_requested, "
    "lease_owner, lease_expires_at, documents_input, documents_after_dedup, clusters_total, "
    "candidates_scored, weak_signals_total, weak_signals_confident, excluded_mature, "
    "excluded_hype_or_noise, excluded_insufficient_evidence, excluded_off_topic, duration_ms, "
    "error_code, error_message, created_at, started_at, finished_at"
)

MODEL_VERSION_COLUMNS = (
    "model_version_id, model_family, feature_schema_version, embedding_model, dataset_version, "
    "artifact_path, artifact_sha256, git_commit, trained_at, threshold, accuracy, precision_score, "
    "recall_score, f1_score, roc_auc, test_size, evaluation_protocol, stage_model_present, "
    "trend_model_present, is_active"
)

CANDIDATE_COLUMNS = (
    "candidate_id, analysis_id, cluster_index, rank, title_auto, keyphrases, score, decision, "
    "decision_reason, decision_explanation_ru, document_count, query_relevance, predicted_stage, "
    "predicted_trend"
)


def row_to_analysis(row: dict[str, Any]) -> Analysis:
    """Строка `analyses` → сущность анализа."""
    lease = (
        Lease(owner=row["lease_owner"], expires_at=row["lease_expires_at"])
        if row["lease_owner"] and row["lease_expires_at"]
        else None
    )
    return Analysis(
        analysis_id=str(row["analysis_id"]),
        idempotency_key=row["idempotency_key"],
        collection_id=str(row["collection_id"]),
        query_text=row["query_text"],
        model_version_id=row["model_version_id"],
        params=AnalysisParams(
            top_n=row["top_n"],
            max_candidates=row["max_candidates"],
            weak_signal_threshold=row["weak_signal_threshold"],
            min_evidence_documents=row["min_evidence_documents"],
        ),
        status=OperationStatus(row["status"]),
        cancel_requested=row["cancel_requested"],
        lease=lease,
        stats=AnalysisStats(
            documents_input=row["documents_input"],
            documents_after_dedup=row["documents_after_dedup"],
            clusters_total=row["clusters_total"],
            candidates_scored=row["candidates_scored"],
            weak_signals_total=row["weak_signals_total"],
            weak_signals_confident=row["weak_signals_confident"],
            excluded_mature=row["excluded_mature"],
            excluded_hype_or_noise=row["excluded_hype_or_noise"],
            excluded_insufficient_evidence=row["excluded_insufficient_evidence"],
            excluded_off_topic=row["excluded_off_topic"],
            duration_ms=row["duration_ms"] or 0,
        ),
        error_code=row["error_code"] or "",
        error_message=row["error_message"] or "",
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def row_to_model_version(row: dict[str, Any]) -> ModelVersion:
    """Строка `model_versions` → версия модели."""
    return ModelVersion(
        model_version_id=row["model_version_id"],
        model_family=row["model_family"],
        feature_schema_version=row["feature_schema_version"],
        embedding_model=row["embedding_model"],
        dataset_version=row["dataset_version"],
        artifact_path=row["artifact_path"],
        artifact_sha256=row["artifact_sha256"],
        trained_at=row["trained_at"],
        metrics=ModelMetrics(
            accuracy=row["accuracy"],
            precision=row["precision_score"],
            recall=row["recall_score"],
            f1=row["f1_score"],
            roc_auc=row["roc_auc"],
            threshold=row["threshold"],
            test_size=row["test_size"],
            evaluation_protocol=row["evaluation_protocol"],
        ),
        git_commit=row["git_commit"] or "",
        stage_model_present=row["stage_model_present"],
        trend_model_present=row["trend_model_present"],
        is_active=row["is_active"],
    )
