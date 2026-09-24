"""Преобразование protobuf ↔ домен insight (контракты 1.0.0)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp
from weaksignals.analyzer.v1 import analyzer_pb2
from weaksignals.common.v1 import common_pb2
from weaksignals.insight.v1 import insight_pb2

from insight.application.dto import GenerateInsightCommand, ProviderState
from insight.application.validation import (
    truncate_evidence_text,
    validate_evidence_count,
    validate_features_count,
    validate_idempotency_key,
    validate_max_output_tokens,
    validate_query_text,
    validate_uuid,
)
from insight.domain.entities import (
    CandidateContext,
    EvidenceDocument,
    FeatureContribution,
    Insight,
    QueryExpansion,
)
from insight.domain.values import Decision, FeatureDirection, InsightStatus, SummaryKind, TrustLevel


def to_timestamp(moment: datetime) -> Timestamp:
    """datetime → google.protobuf.Timestamp."""
    timestamp = Timestamp()
    timestamp.FromDatetime(moment)
    return timestamp


def from_generate_request(request: insight_pb2.GenerateInsightRequest) -> GenerateInsightCommand:
    """`GenerateInsightRequest` → валидированная команда сценария."""
    candidate = request.candidate
    validate_features_count(len(candidate.top_features))
    validate_evidence_count(len(request.evidence))
    context = CandidateContext(
        candidate_id=validate_uuid(candidate.candidate_id, "candidate.candidate_id"),
        title=candidate.title.strip() or "Без названия",
        keyphrases=tuple(candidate.keyphrases),
        score=float(candidate.score),
        decision=Decision[
            analyzer_pb2.Decision.Name(candidate.decision).removeprefix("DECISION_")
        ],
        query_text=validate_query_text(candidate.query_text),
        top_features=tuple(
            FeatureContribution(
                feature_name=feature.feature_name,
                label_ru=feature.label_ru,
                value=feature.value,
                contribution=feature.contribution,
                direction=FeatureDirection(feature.direction),
            )
            for feature in candidate.top_features
        ),
    )
    evidence = tuple(
        EvidenceDocument(
            document_id=validate_uuid(document.document_id, "evidence.document_id"),
            title=document.title,
            url=document.url,
            text=truncate_evidence_text(document.text),
            language_code=document.language_code or "en",
            source_type=common_pb2.SourceType.Name(document.source_type).removeprefix("SOURCE_TYPE_"),
            trust_level=TrustLevel[
                common_pb2.TrustLevel.Name(document.trust_level).removeprefix("TRUST_LEVEL_")
            ],
            published_at=document.published_at.ToDatetime(tzinfo=UTC) if document.HasField("published_at") else None,
        )
        for document in request.evidence
    )
    return GenerateInsightCommand(
        idempotency_key=validate_idempotency_key(request.idempotency_key),
        candidate=context,
        evidence=evidence,
        allow_fallback=request.allow_fallback,
        max_output_tokens=validate_max_output_tokens(request.max_output_tokens),
    )


def to_generate_response(insight: Insight) -> insight_pb2.GenerateInsightResponse:
    """Инсайт домена → `GenerateInsightResponse`."""
    narrative = insight.narrative
    provenance = insight.provenance
    return insight_pb2.GenerateInsightResponse(
        insight_id=insight.insight_id,
        status=insight_pb2.InsightStatus.Value(f"INSIGHT_STATUS_{insight.status.name}"),
        narrative=insight_pb2.Narrative(
            title_ru=narrative.title_ru,
            description_ru=narrative.description_ru,
            advantage_ru=narrative.advantage_ru,
            case_example_ru=narrative.case_example_ru,
            case_document_id=narrative.case_document_id,
            explanation_ru=narrative.explanation_ru,
        ),
        source_summaries=[
            insight_pb2.SourceSummary(
                document_id=summary.document_id,
                summary_ru=summary.summary_ru,
                kind=insight_pb2.SummaryKind.Value(f"SUMMARY_KIND_{summary.kind.name}"),
            )
            for summary in insight.summaries
        ],
        grounding=insight_pb2.GroundingCheck(
            passed=insight.grounding.passed,
            unsupported_numbers=insight.grounding.unsupported_numbers,
            unknown_document_refs=insight.grounding.unknown_document_refs,
            features_mentioned=insight.grounding.features_mentioned,
        ),
        provenance=to_provenance(provenance),
        from_cache=insight.from_cache,
    )


def to_provenance(provenance) -> insight_pb2.Provenance:  # noqa: ANN001 - domain Provenance
    """Происхождение ответа → `insight.v1.Provenance`."""
    return insight_pb2.Provenance(
        provider=provenance.provider,
        model=provenance.model,
        prompt_version=provenance.prompt_version,
        prompt_tokens=provenance.prompt_tokens,
        completion_tokens=provenance.completion_tokens,
        attempts=provenance.attempts,
        latency_ms=provenance.latency_ms,
    )


def to_expand_response(expansion: QueryExpansion) -> insight_pb2.ExpandQueryResponse:
    """Расширение запроса → `ExpandQueryResponse`."""
    return insight_pb2.ExpandQueryResponse(
        ru_terms=list(expansion.ru_terms),
        en_terms=list(expansion.en_terms),
        domain_tags=list(expansion.domain_tags),
        provenance=to_provenance(expansion.provenance),
        used_fallback=expansion.used_fallback,
    )


def to_provider_status_response(
    states: Sequence[ProviderState], active: str
) -> insight_pb2.GetProviderStatusResponse:
    """Состояния провайдеров → `GetProviderStatusResponse`."""
    response = insight_pb2.GetProviderStatusResponse(active_provider=active)
    for state in states:
        provider = response.providers.add()
        provider.provider = state.provider
        provider.enabled = state.enabled
        provider.healthy = state.healthy
        provider.max_concurrency = state.max_concurrency
        provider.in_flight = state.in_flight
        provider.queued = state.queued
        provider.last_error = state.last_error
        if state.last_success_at is not None:
            provider.last_success_at.CopyFrom(to_timestamp(state.last_success_at))
    return response


def status_name(status: InsightStatus) -> str:
    """Имя статуса для журналов."""
    return status.value


def summary_kind_name(kind: SummaryKind) -> str:
    """Имя вида резюме для журналов."""
    return kind.value
