"""Преобразование сообщений protobuf в DTO orchestrator (маппинг рядом с клиентами, §8)."""

from __future__ import annotations

from datetime import UTC

from typing import Any

from weaksignals.analyzer.v1 import analyzer_pb2
from weaksignals.common.v1 import common_pb2

from orchestrator.application.dto import (
    AnalysisView,
    CandidateView,
    CollectionView,
    DocumentView,
    EvidenceView,
    FeatureView,
    ModelInfoView,
    ScoreTextView,
)
from orchestrator.domain.values import Decision, FeatureDirection, TrustLevel

TERMINAL_OPERATION_STATUSES = frozenset(
    {
        common_pb2.OPERATION_STATUS_COMPLETED,
        common_pb2.OPERATION_STATUS_PARTIAL,
        common_pb2.OPERATION_STATUS_FAILED,
        common_pb2.OPERATION_STATUS_CANCELLED,
    }
)


def status_name(value: int) -> str:
    """`common.v1.OperationStatus` → короткое имя статуса."""
    return common_pb2.OperationStatus.Name(value).removeprefix("OPERATION_STATUS_")


def source_type_name(value: int) -> str:
    """`common.v1.SourceType` → короткое имя типа источника."""
    return common_pb2.SourceType.Name(value).removeprefix("SOURCE_TYPE_")


def source_key_name(value: int) -> str:
    """`common.v1.SourceKey` → короткое имя источника в нижнем регистре."""
    return common_pb2.SourceKey.Name(value).removeprefix("SOURCE_KEY_").lower()


def trust_level(value: int) -> TrustLevel:
    """`common.v1.TrustLevel` → доменное значение."""
    return TrustLevel[common_pb2.TrustLevel.Name(value).removeprefix("TRUST_LEVEL_")]


def to_collection_view(response: Any) -> CollectionView:
    """`GetCollectionResponse` → состояние коллекции."""
    failed = tuple(
        source_key_name(run.source_key)
        for run in getattr(response, "adapter_runs", ())
        if status_name(run.status) == "FAILED"
    )
    completed = sum(
        1 for run in getattr(response, "adapter_runs", ()) if status_name(run.status) == "COMPLETED"
    )
    return CollectionView(
        collection_id=response.collection_id,
        status=status_name(response.status),
        documents_total=response.documents_total,
        http_requests_total=getattr(response, "http_requests_total", 0),
        adapters_completed=completed,
        failed_adapters=failed,
        is_terminal=response.status in TERMINAL_OPERATION_STATUSES,
    )


def to_analysis_view(response: Any) -> AnalysisView:
    """`GetAnalysisResponse` → состояние анализа."""
    stats = response.stats
    return AnalysisView(
        analysis_id=response.analysis_id,
        status=status_name(response.status),
        model_version_id=response.model_version_id,
        candidates_scored=stats.candidates_scored,
        weak_signals_total=stats.weak_signals_total,
        weak_signals_confident=stats.weak_signals_confident,
        error_code=response.error_code,
        error_message=response.error_message,
        is_terminal=response.status in TERMINAL_OPERATION_STATUSES,
    )


def to_feature_view(feature: Any) -> FeatureView:
    """`analyzer.v1.FeatureContribution` → признак выдачи."""
    return FeatureView(
        feature_name=feature.feature_name,
        label_ru=feature.label_ru,
        value=feature.value,
        contribution=feature.contribution,
        direction=FeatureDirection(feature.direction),
    )


def to_candidate_view(candidate: Any) -> CandidateView:
    """`analyzer.v1.Candidate` → кандидат."""
    return CandidateView(
        candidate_id=candidate.candidate_id,
        rank=candidate.rank,
        title_auto=candidate.title,
        keyphrases=tuple(candidate.keyphrases),
        score=candidate.score,
        decision=Decision[
            analyzer_pb2.Decision.Name(candidate.decision).removeprefix("DECISION_")
        ],
        decision_reason=analyzer_pb2.DecisionReason.Name(candidate.decision_reason).removeprefix(
            "DECISION_REASON_"
        ),
        decision_explanation_ru=candidate.decision_explanation_ru,
        document_count=candidate.document_count,
        features=tuple(to_feature_view(feature) for feature in candidate.features),
        evidence=tuple(
            EvidenceView(
                document_id=item.document_id,
                snippet=item.snippet,
                similarity=item.similarity,
                source_type=source_type_name(item.source_type),
                trust_level=trust_level(item.trust_level),
            )
            for item in candidate.evidence
        ),
        predicted_stage=candidate.predicted_stage if candidate.HasField("predicted_stage") else None,
        predicted_trend=candidate.predicted_trend if candidate.HasField("predicted_trend") else None,
    )


def to_document_view(document: Any) -> DocumentView:
    """`common.v1.Document` → документ для доказательств и источников."""
    return DocumentView(
        document_id=document.document_id,
        title=document.title,
        url=document.url,
        text=document.text,
        language_code=document.language_code,
        source_type=source_type_name(document.source_type),
        source_key=source_key_name(document.source_key),
        trust_level=trust_level(document.trust_level),
        published_at=document.published_at.ToDatetime(tzinfo=UTC) if document.HasField("published_at") else None,
    )


def to_model_info_view(response: Any) -> ModelInfoView:
    """`GetModelInfoResponse` → сведения о модели."""
    metrics = response.metrics
    return ModelInfoView(
        model_version_id=response.model_version_id,
        model_family=response.model_family,
        feature_schema_version=response.feature_schema_version,
        embedding_model=response.embedding_model,
        dataset_version=response.dataset_version,
        trained_at=response.trained_at.ToDatetime(tzinfo=UTC),
        metrics={
            "accuracy": metrics.accuracy,
            "precision": metrics.precision,
            "recall": metrics.recall,
            "f1": metrics.f1,
            "roc_auc": metrics.roc_auc,
            "threshold": metrics.threshold,
            "test_size": float(metrics.test_size),
        },
        feature_names=tuple(response.feature_names),
    )


def to_score_view(response: Any) -> ScoreTextView:
    """`ScoreTextResponse` → результат прямого скоринга."""
    return ScoreTextView(
        score=response.score,
        decision=Decision[analyzer_pb2.Decision.Name(response.decision).removeprefix("DECISION_")],
        decision_reason=analyzer_pb2.DecisionReason.Name(response.decision_reason).removeprefix(
            "DECISION_REASON_"
        ),
        features=tuple(to_feature_view(feature) for feature in response.features),
        model_version_id=response.model_version_id,
        enrichment_applied=response.enrichment_applied,
        predicted_stage=response.predicted_stage if response.HasField("predicted_stage") else None,
        predicted_trend=response.predicted_trend if response.HasField("predicted_trend") else None,
    )
