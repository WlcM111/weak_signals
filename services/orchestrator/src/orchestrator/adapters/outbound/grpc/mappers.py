"""Преобразование сообщений protobuf в DTO orchestrator (маппинг рядом с клиентами, §8)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC

from typing import Any

from urllib.parse import urlsplit

from weaksignals.analyzer.v1 import analyzer_pb2
from weaksignals.common.v1 import common_pb2
from weaksignals.insight.v1 import insight_pb2

from orchestrator.application.dto import (
    AnalysisView,
    CandidateView,
    CollectionView,
    DocumentView,
    EvidenceView,
    FeatureView,
    FinalizeRequestCard,
    FinalizedCardView,
    JudgeVerdictView,
    ModelInfoView,
    NarrativeView,
    RubricRequestItem,
    ScoreTextView,
)
from orchestrator.domain.values import Decision, FeatureDirection, NarrativeStatus, SummaryKind, TrustLevel

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
    # Этап 5.1: источник, отключённый настройкой или без ключа (DISABLED, AUTH_MISSING), — не отказ этого сбора
    failed = tuple(
        source_key_name(run.source_key)
        for run in getattr(response, "adapter_runs", ())
        if status_name(run.status) == "FAILED" and getattr(run, "error_code", "") not in CONFIGURATION_CODES
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


# --- Запросы и ответы insight: общие для gRPC-клиента и повтора режима А (одинаковые промпты и хеши запросов) ---


def judge_source(document: DocumentView, snippet_chars: int) -> insight_pb2.JudgeSource:
    """Документ → источник для рубричной оценки и доводки (полный контекст, фрагмент ограничен)."""
    published = document.published_at.date().isoformat() if document.published_at else ""
    host = urlsplit(document.url or "").hostname or ""
    return insight_pb2.JudgeSource(
        document_id=document.document_id,
        title=(document.title or "")[:300],
        source_key=document.source_key,
        source_type=document.source_type,
        trust_level=str(getattr(document.trust_level, "value", document.trust_level)),
        published=published,
        language_code=document.language_code,
        snippet=" ".join((document.text or "").split())[:snippet_chars],
        domain=host.removeprefix("www."),
    )


def judge_request(query_text: str, candidates: Sequence[CandidateView]) -> insight_pb2.JudgeCandidatesRequest:
    """Смысловая оценка (прежний режим отбора)."""
    return insight_pb2.JudgeCandidatesRequest(
        query_text=query_text,
        items=[
            insight_pb2.JudgeItem(
                candidate_id=candidate.candidate_id,
                title=candidate.title_auto,
                keyphrases=list(candidate.keyphrases[:8]),
                evidence=[item.snippet[:300] for item in candidate.evidence[:3]],
            )
            for candidate in candidates
        ],
    )


def judge_verdicts(response: Any) -> dict[str, JudgeVerdictView]:
    """Ответ смысловой оценки → вердикты по кандидатам."""
    return {
        verdict.candidate_id: JudgeVerdictView(
            verdict.candidate_id, verdict.verdict, verdict.relevance, verdict.reason_ru
        )
        for verdict in response.verdicts
    }


def rubric_request(
    query_text: str, items: Sequence[RubricRequestItem], final_check: bool = False
) -> insight_pb2.JudgeCandidatesRequest:
    """Рубричная оценка (режим rubric_v2) или финальная проверка готовой карточки (rubric_final)."""
    return insight_pb2.JudgeCandidatesRequest(
        query_text=query_text,
        mode="rubric_final" if final_check else "rubric_v2",
        items=[
            insight_pb2.JudgeItem(
                candidate_id=item.candidate.candidate_id,
                title=item.candidate.title_auto,
                keyphrases=list(item.candidate.keyphrases[:8]),
                sources=[judge_source(document, 400) for document in item.documents[:6]],
                composition_ru=item.composition_ru[:600],
            )
            for item in items
        ],
    )


def rubric_verdicts(response: Any) -> dict[str, JudgeVerdictView]:
    """Ответ рубрики → вердикты; неполные вердикты (без кода, стадии или тренда в шкале) отбрасываются."""
    return {
        v.candidate_id: JudgeVerdictView(
            v.candidate_id, v.verdict, v.relevance, v.reason_ru, code=v.code, on_topic=v.on_topic,
            concrete=v.concrete, early_stage=v.early_stage, verifiable=v.verifiable, stage=v.stage,
            trend=v.trend, confidence=round(float(v.confidence), 4),
            technology_ru=v.technology_ru, profile_ru=v.profile_ru,
        )
        for v in response.verdicts
        if v.code and 1 <= v.stage <= 4 and 1 <= v.trend <= 3
    }


def finalize_request(query_text: str, cards: Sequence[FinalizeRequestCard]) -> insight_pb2.FinalizeCardsRequest:
    """Пакетная доводка карточек."""
    return insight_pb2.FinalizeCardsRequest(
        query_text=query_text,
        cards=[
            insight_pb2.FinalizeCard(
                candidate_id=card.candidate.candidate_id,
                title_auto=card.candidate.title_auto,
                keyphrases=list(card.candidate.keyphrases[:8]),
                sources=[judge_source(document, 1500) for document in card.documents[:5]],
                stage=card.stage,
                trend=card.trend,
                judge_reason_ru=card.judge_reason_ru[:300],
            )
            for card in cards
        ],
    )


def finalized_cards(response: Any) -> dict[str, FinalizedCardView]:
    """Ответ доводки → карточки, прошедшие проверку по источникам."""
    return {
        card.candidate_id: FinalizedCardView(
            candidate_id=card.candidate_id, title_ru=card.title_ru, description_ru=card.description_ru,
            advantage_ru=card.advantage_ru, case_example_ru=card.case_example_ru,
            case_document_id=card.case_document_id, why_ru=card.why_ru, companies=tuple(card.companies),
            stage=card.stage, trend=card.trend, stage_reason_ru=card.stage_reason_ru,
            trend_reason_ru=card.trend_reason_ru,
            source_summaries={
                summary.document_id: (summary.summary_ru, insight_pb2.SummaryKind.Name(summary.kind).removeprefix("SUMMARY_KIND_"))
                for summary in card.source_summaries
            },
            llm_provider=response.provider, llm_model=response.model,
            prompt_version=response.prompt_version or "finalize_v1",
        )
        for card in response.cards
        if card.title_ru
    }


def generate_request(
    idempotency_key: str, candidate: CandidateView, query_text: str, evidence: Sequence[DocumentView]
) -> insight_pb2.GenerateInsightRequest:
    """Нарратив кандидата по переданным доказательствам."""
    return insight_pb2.GenerateInsightRequest(
        idempotency_key=idempotency_key,
        candidate=insight_pb2.CandidateContext(
            candidate_id=candidate.candidate_id,
            title=candidate.title_auto,
            keyphrases=list(candidate.keyphrases),
            score=candidate.score,
            decision=analyzer_pb2.Decision.Value(f"DECISION_{candidate.decision.name}"),
            top_features=[
                analyzer_pb2.FeatureContribution(
                    feature_name=feature.feature_name,
                    value=feature.value,
                    contribution=feature.contribution,
                    label_ru=feature.label_ru,
                    direction=feature.direction.value,
                )
                for feature in candidate.features[:8]
            ],
            query_text=query_text,
        ),
        evidence=[
            insight_pb2.EvidenceDocument(
                document_id=document.document_id,
                title=document.title,
                url=document.url,
                text=document.text,
                language_code=document.language_code,
                source_type=common_pb2.SourceType.Value(f"SOURCE_TYPE_{document.source_type}"),
                trust_level=common_pb2.TrustLevel.Value(f"TRUST_LEVEL_{document.trust_level.value}"),
            )
            for document in evidence
        ],
        allow_fallback=True,
    )


def narrative_view(response: Any, prompt_version: str) -> NarrativeView:
    """Ответ генерации нарратива → нарратив элемента выдачи."""
    narrative = response.narrative
    provenance = response.provenance
    status = (
        NarrativeStatus.GENERATED
        if insight_pb2.InsightStatus.Name(response.status) == "INSIGHT_STATUS_GENERATED"
        else NarrativeStatus.FALLBACK_EXTRACTIVE
    )
    return NarrativeView(
        title_ru=narrative.title_ru,
        description_ru=narrative.description_ru,
        advantage_ru=narrative.advantage_ru,
        case_example_ru=narrative.case_example_ru,
        explanation_ru=narrative.explanation_ru,
        status=status.value,
        llm_provider=provenance.provider,
        llm_model=provenance.model,
        prompt_version=provenance.prompt_version or prompt_version,
        case_document_id=narrative.case_document_id,
        source_summaries={
            summary.document_id: (
                summary.summary_ru,
                SummaryKind[insight_pb2.SummaryKind.Name(summary.kind).removeprefix("SUMMARY_KIND_")].value,
            )
            for summary in response.source_summaries
        },
    )


CONFIGURATION_CODES = frozenset({"DISABLED", "AUTH_MISSING"})
