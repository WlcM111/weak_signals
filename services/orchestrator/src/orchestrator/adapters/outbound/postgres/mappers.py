"""Преобразование строк PostgreSQL в сущности orchestrator."""

from __future__ import annotations

from typing import Any

from orchestrator.domain.entities import (
    ExcludedCandidate,
    FeatureRow,
    Job,
    ResultItem,
    SourceRow,
)
from orchestrator.domain.values import (
    Decision,
    FeatureDirection,
    JobStats,
    JobStatus,
    Lease,
    NarrativeStatus,
    SummaryKind,
    TrustLevel,
)

JOB_COLUMNS = (
    "j.job_id, j.query_id, j.status, j.attempt, j.cancel_requested, j.lease_owner, "
    "j.lease_expires_at, j.collection_id, j.analysis_id, j.error_code, j.error_message, "
    "j.created_at, j.started_at, j.finished_at, q.query_text, q.requested_top_n"
)

ITEM_COLUMNS = (
    "item_id, job_id, rank, candidate_id, title_ru, title_auto, score, decision_reason, "
    "decision_explanation_ru, description_ru, advantage_ru, case_example_ru, case_document_id, "
    "explanation_ru, narrative_status, llm_provider, llm_model, prompt_version, predicted_stage, "
    "predicted_trend, document_count, created_at"
)

STATS_COLUMNS = (
    "http_requests_total, sources_processed, documents_collected, candidates_found, "
    "weak_signals_total, weak_signals_confident, collect_ms, analyze_ms, narrate_ms, "
    "narratives_generated, narratives_fallback, model_version_id, expand_used_fallback"
)


def row_to_job(row: dict[str, Any]) -> Job:
    """Строка `jobs` + текст запроса → сущность задания."""
    lease = (
        Lease(owner=row["lease_owner"], expires_at=row["lease_expires_at"])
        if row.get("lease_owner") and row.get("lease_expires_at")
        else None
    )
    return Job(
        job_id=str(row["job_id"]),
        query_id=str(row["query_id"]),
        status=JobStatus(row["status"]),
        attempt=row["attempt"],
        cancel_requested=row["cancel_requested"],
        lease=lease,
        collection_id=str(row["collection_id"]) if row.get("collection_id") else "",
        analysis_id=str(row["analysis_id"]) if row.get("analysis_id") else "",
        error_code=row.get("error_code") or "",
        error_message=row.get("error_message") or "",
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        query_text=row.get("query_text") or "",
        requested_top_n=row.get("requested_top_n") or 15,
    )


def row_to_stats(row: dict[str, Any] | None) -> JobStats:
    """Строка `job_stats` → статистика; отсутствие строки даёт нули."""
    if row is None:
        return JobStats()
    return JobStats(
        http_requests_total=row["http_requests_total"],
        sources_processed=row["sources_processed"],
        documents_collected=row["documents_collected"],
        candidates_found=row["candidates_found"],
        weak_signals_total=row["weak_signals_total"],
        weak_signals_confident=row["weak_signals_confident"],
        collect_ms=row["collect_ms"],
        analyze_ms=row["analyze_ms"],
        narrate_ms=row["narrate_ms"],
        narratives_generated=row["narratives_generated"],
        narratives_fallback=row["narratives_fallback"],
        model_version_id=row["model_version_id"] or "",
        expand_used_fallback=row["expand_used_fallback"],
    )


def row_to_item(
    row: dict[str, Any], features: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> ResultItem:
    """Строки трёх таблиц → элемент выдачи."""
    return ResultItem(
        item_id=str(row["item_id"]),
        job_id=str(row["job_id"]),
        rank=row["rank"],
        candidate_id=str(row["candidate_id"]),
        title_ru=row["title_ru"],
        title_auto=row["title_auto"],
        score=float(row["score"]),
        decision_reason=row["decision_reason"],
        decision_explanation_ru=row["decision_explanation_ru"],
        description_ru=row["description_ru"],
        advantage_ru=row["advantage_ru"],
        case_example_ru=row["case_example_ru"],
        case_document_id=str(row["case_document_id"]) if row.get("case_document_id") else "",
        explanation_ru=row["explanation_ru"],
        narrative_status=NarrativeStatus(row["narrative_status"]),
        llm_provider=row["llm_provider"],
        llm_model=row["llm_model"],
        prompt_version=row["prompt_version"],
        predicted_stage=row["predicted_stage"],
        predicted_trend=row["predicted_trend"],
        document_count=row["document_count"],
        created_at=row.get("created_at"),
        features=tuple(
            FeatureRow(
                feature_name=item["feature_name"],
                label_ru=item["label_ru"],
                value=float(item["value"]),
                contribution=float(item["contribution"]),
                direction=FeatureDirection(item["direction"]),
                display_order=item["display_order"],
            )
            for item in sorted(features, key=lambda value: value["display_order"])
        ),
        sources=tuple(
            SourceRow(
                position=item["position"],
                document_id=str(item["document_id"]),
                title=item["title"],
                url=item["url"],
                published_at=item["published_at"],
                source_type=item["source_type"],
                source_key=item["source_key"],
                language_code=item["language_code"],
                trust_level=TrustLevel(item["trust_level"]),
                summary_ru=item["summary_ru"],
                summary_kind=SummaryKind(item["summary_kind"]),
                snippet=item["snippet"],
                similarity=float(item["similarity"]),
            )
            for item in sorted(sources, key=lambda value: value["position"])
        ),
    )


def row_to_excluded(row: dict[str, Any]) -> ExcludedCandidate:
    """Строка `excluded_candidates` → исключённый кандидат."""
    return ExcludedCandidate(
        candidate_id=str(row["candidate_id"]),
        title_auto=row["title_auto"],
        score=float(row["score"]),
        decision=Decision(row["decision"]),
        decision_reason=row["decision_reason"],
        decision_explanation_ru=row["decision_explanation_ru"],
        document_count=row["document_count"],
    )
