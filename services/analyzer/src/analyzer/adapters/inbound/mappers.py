"""Преобразование доменных объектов analyzer в сообщения protobuf (контракты 1.0.0).

Перечисления домена и proto связаны по имени члена: `Decision.MATURE` ↔ `DECISION_MATURE`.
Совпадение проверяется `tools/check_proto_conformance.py`.
"""

from __future__ import annotations

from datetime import datetime

from google.protobuf.timestamp_pb2 import Timestamp
from weaksignals.analyzer.v1 import analyzer_pb2
from weaksignals.common.v1 import common_pb2

from analyzer.application.dto import AnalysisView, ScoreTextResult
from analyzer.domain.entities import Candidate, FeatureContribution, ModelVersion
from analyzer.domain.values import (
    AnalysisStats,
    Decision,
    DecisionReason,
    OperationStatus,
    SourceType,
    TrustLevel,
)


def to_timestamp(moment: datetime) -> Timestamp:
    """datetime → google.protobuf.Timestamp (UTC)."""
    timestamp = Timestamp()
    timestamp.FromDatetime(moment)
    return timestamp


def to_proto_status(status: OperationStatus) -> int:
    """Статус операции → `common.v1.OperationStatus`."""
    return int(common_pb2.OperationStatus.Value(f"OPERATION_STATUS_{status.name}"))


def to_proto_decision(decision: Decision) -> int:
    """Решение → `analyzer.v1.Decision`."""
    return int(analyzer_pb2.Decision.Value(f"DECISION_{decision.name}"))


def to_proto_reason(reason: DecisionReason) -> int:
    """Причина решения → `analyzer.v1.DecisionReason`."""
    return int(analyzer_pb2.DecisionReason.Value(f"DECISION_REASON_{reason.name}"))


def to_proto_source_type(source_type: SourceType) -> int:
    """Тип источника → `common.v1.SourceType`."""
    return int(common_pb2.SourceType.Value(f"SOURCE_TYPE_{source_type.name}"))


def to_proto_trust_level(trust_level: TrustLevel) -> int:
    """Уровень доверенности → `common.v1.TrustLevel`."""
    return int(common_pb2.TrustLevel.Value(f"TRUST_LEVEL_{trust_level.name}"))


def to_proto_stats(stats: AnalysisStats) -> analyzer_pb2.AnalysisStats:
    """Статистика анализа → `AnalysisStats`."""
    return analyzer_pb2.AnalysisStats(
        documents_input=stats.documents_input,
        documents_after_dedup=stats.documents_after_dedup,
        clusters_total=stats.clusters_total,
        candidates_scored=stats.candidates_scored,
        weak_signals_total=stats.weak_signals_total,
        weak_signals_confident=stats.weak_signals_confident,
        excluded_mature=stats.excluded_mature,
        excluded_hype_or_noise=stats.excluded_hype_or_noise,
        excluded_insufficient_evidence=stats.excluded_insufficient_evidence,
        excluded_off_topic=stats.excluded_off_topic,
        duration_ms=stats.duration_ms,
    )


def to_proto_analysis(view: AnalysisView) -> analyzer_pb2.GetAnalysisResponse:
    """Состояние анализа → `GetAnalysisResponse`."""
    response = analyzer_pb2.GetAnalysisResponse(
        analysis_id=view.analysis_id,
        status=to_proto_status(view.status),
        model_version_id=view.model_version_id,
        stats=to_proto_stats(view.stats),
        error_code=view.error_code,
        error_message=view.error_message,
    )
    if view.started_at is not None:
        response.started_at.CopyFrom(to_timestamp(view.started_at))
    if view.finished_at is not None:
        response.finished_at.CopyFrom(to_timestamp(view.finished_at))
    return response


def to_proto_feature(feature: FeatureContribution) -> analyzer_pb2.FeatureContribution:
    """Вклад признака → `FeatureContribution`."""
    return analyzer_pb2.FeatureContribution(
        feature_name=feature.feature_name,
        value=feature.value,
        contribution=feature.contribution,
        label_ru=feature.label_ru,
        direction=feature.direction.value,
    )


def to_proto_candidate(candidate: Candidate, analysis_id: str) -> analyzer_pb2.Candidate:
    """Кандидат домена → `Candidate`."""
    message = analyzer_pb2.Candidate(
        candidate_id=candidate.candidate_id,
        analysis_id=analysis_id,
        rank=candidate.rank,
        title=candidate.title_auto,
        keyphrases=list(candidate.keyphrases),
        score=candidate.score,
        decision=to_proto_decision(candidate.decision),
        decision_reason=to_proto_reason(candidate.decision_reason),
        decision_explanation_ru=candidate.decision_explanation_ru,
        features=[to_proto_feature(feature) for feature in candidate.features],
        evidence=[
            analyzer_pb2.Evidence(
                document_id=item.document_id,
                snippet=item.snippet,
                similarity=item.similarity,
                source_type=to_proto_source_type(item.source_type),
                trust_level=to_proto_trust_level(item.trust_level),
            )
            for item in candidate.evidence
        ],
        document_count=candidate.document_count,
        source_type_counts=[
            analyzer_pb2.SourceTypeCount(source_type=to_proto_source_type(source_type), count=count)
            for source_type, count in sorted(
                candidate.source_type_counts.items(), key=lambda item: (-item[1], item[0].value)
            )
        ],
        year_counts=[
            analyzer_pb2.YearCount(year=year, count=count)
            for year, count in sorted(candidate.year_counts.items())
        ],
        query_relevance=candidate.query_relevance,
    )
    if candidate.predicted_stage is not None:
        message.predicted_stage = candidate.predicted_stage
    if candidate.predicted_trend is not None:
        message.predicted_trend = candidate.predicted_trend
    return message


def to_proto_score_text(result: ScoreTextResult) -> analyzer_pb2.ScoreTextResponse:
    """Результат прямого скоринга → `ScoreTextResponse`."""
    response = analyzer_pb2.ScoreTextResponse(
        score=result.score,
        decision=to_proto_decision(result.decision),
        decision_reason=to_proto_reason(result.decision_reason),
        features=[to_proto_feature(feature) for feature in result.features],
        model_version_id=result.model_version_id,
        enrichment_applied=result.enrichment_applied,
    )
    if result.predicted_stage is not None:
        response.predicted_stage = result.predicted_stage
    if result.predicted_trend is not None:
        response.predicted_trend = result.predicted_trend
    return response


def to_proto_model_info(
    version: ModelVersion, feature_names: tuple[str, ...]
) -> analyzer_pb2.GetModelInfoResponse:
    """Версия модели → `GetModelInfoResponse`."""
    metrics = version.metrics
    return analyzer_pb2.GetModelInfoResponse(
        model_version_id=version.model_version_id,
        model_family=version.model_family,
        feature_schema_version=version.feature_schema_version,
        embedding_model=version.embedding_model,
        dataset_version=version.dataset_version,
        trained_at=to_timestamp(version.trained_at),
        metrics=analyzer_pb2.ModelMetrics(
            accuracy=metrics.accuracy,
            precision=metrics.precision,
            recall=metrics.recall,
            f1=metrics.f1,
            roc_auc=metrics.roc_auc,
            threshold=metrics.threshold,
            test_size=metrics.test_size,
            evaluation_protocol=metrics.evaluation_protocol,
        ),
        feature_names=list(feature_names),
    )
