"""Преобразование доменных объектов в тела ответов `orchestrator.openapi.yaml`.

Слой отделён от FastAPI: он возвращает обычные словари, поэтому соответствие контракту
проверяется тестами без поднятия веб-сервера.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orchestrator.application.dto import (
    JobView,
    ModelInfoView,
    ResultsView,
    ScoreTextView,
)
from orchestrator.domain.entities import ExcludedCandidate, FeatureRow, ResultItem, SourceRow


def _moment(value: datetime | None) -> str | None:
    """Время в ISO 8601 или None."""
    return value.isoformat() if value else None


def job_to_json(view: JobView) -> dict[str, Any]:
    """Схема `Job` с вложенным прогрессом."""
    job = view.job
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "query_id": job.query_id,
        "query_text": job.query_text,
        "status": job.status.value,
        "attempt": job.attempt,
        "cancel_requested": job.cancel_requested,
        "created_at": _moment(job.created_at),
        "started_at": _moment(job.started_at),
        "finished_at": _moment(job.finished_at),
        "progress": {
            "stage": view.progress.stage.value,
            "http_requests_total": view.progress.http_requests_total,
            "sources_processed": view.progress.sources_processed,
            "documents_collected": view.progress.documents_collected,
            "candidates_found": view.progress.candidates_found,
            "narratives_done": view.progress.narratives_done,
            "narratives_total": view.progress.narratives_total,
        },
    }
    if job.error_code:
        payload["error"] = {
            "code": job.error_code,
            "message": job.error_message,
            "request_id": "",
            "retryable": False,
        }
    return payload


def feature_to_json(feature: FeatureRow) -> dict[str, Any]:
    """Схема `Feature`."""
    return {
        "feature_name": feature.feature_name,
        "label_ru": feature.label_ru,
        "value": feature.value,
        "contribution": feature.contribution,
        "direction": feature.direction.value,
    }


def source_to_json(source: SourceRow) -> dict[str, Any]:
    """Схема `Source` — все поля, которых требует ТЗ от источника."""
    return {
        "document_id": source.document_id,
        "title": source.title,
        "url": source.url,
        "published_at": _moment(source.published_at),
        "source_type": source.source_type,
        "source_key": source.source_key,
        "language_code": source.language_code,
        "trust_level": source.trust_level.value,
        "summary_ru": source.summary_ru,
        "summary_kind": source.summary_kind.value,
        "snippet": source.snippet,
        "similarity": source.similarity,
    }


def item_summary_to_json(item: ResultItem) -> dict[str, Any]:
    """Схема `ResultItemSummary` для списка выдачи."""
    return {
        "item_id": item.item_id,
        "rank": item.rank,
        "title_ru": item.title_ru,
        "score": float(item.score),
        "confidence_band": item.confidence_band.value,
        "key_predictors": [feature_to_json(feature) for feature in item.key_predictors],
        "decision_explanation_ru": item.decision_explanation_ru,
        "narrative_status": item.narrative_status.value,
        "source_count": len(item.sources),
    }


def item_to_json(item: ResultItem, query_text: str, model_version_id: str = "") -> dict[str, Any]:
    """Схема `ResultItem`: сводка плюс полный нарратив, признаки, источники и происхождение."""
    payload = item_summary_to_json(item)
    payload.update(
        {
            "job_id": item.job_id,
            "query_text": query_text,
            "title_auto": item.title_auto,
            "description_ru": item.description_ru,
            "advantage_ru": item.advantage_ru,
            "case_example_ru": item.case_example_ru,
            "explanation_ru": item.explanation_ru,
            "features": [feature_to_json(feature) for feature in item.features],
            "sources": [source_to_json(source) for source in item.sources],
            "provenance": {
                "llm_provider": item.llm_provider,
                "llm_model": item.llm_model,
                "prompt_version": item.prompt_version,
                # Версия модели analyzer из статистики задания (раньше поле было всегда пустым: «—» в интерфейсе).
                "model_version_id": model_version_id,
            },
        }
    )
    if item.case_document_id:
        payload["case_document_id"] = item.case_document_id
    if item.predicted_stage is not None:
        payload["predicted_stage"] = item.predicted_stage
    if item.predicted_trend is not None:
        payload["predicted_trend"] = item.predicted_trend
    return payload


def excluded_to_json(candidate: ExcludedCandidate) -> dict[str, Any]:
    """Схема `ExcludedCandidate` — причины исключения зрелых и нерелевантных кандидатов."""
    return {
        "candidate_id": candidate.candidate_id,
        "title_auto": candidate.title_auto,
        "score": float(candidate.score),
        "decision": candidate.decision.value,
        "decision_reason": candidate.decision_reason,
        "decision_explanation_ru": candidate.decision_explanation_ru,
        "document_count": candidate.document_count,
    }


def results_to_json(view: ResultsView) -> dict[str, Any]:
    """Схема `Results`."""
    stats = view.stats
    return {
        "job_id": view.job.job_id,
        "query_text": view.query_text,
        "status": view.job.status.value,
        "items": [item_summary_to_json(item) for item in view.items],
        "excluded": [excluded_to_json(candidate) for candidate in view.excluded],
        "stats": {
            "http_requests_total": stats.http_requests_total,
            "sources_processed": stats.sources_processed,
            "documents_collected": stats.documents_collected,
            "candidates_found": stats.candidates_found,
            "weak_signals_total": stats.weak_signals_total,
            "weak_signals_confident": stats.weak_signals_confident,
            "narratives_generated": stats.narratives_generated,
            "narratives_fallback": stats.narratives_fallback,
            "model_version_id": stats.model_version_id,
            "expand_used_fallback": stats.expand_used_fallback,
        },
    }


def model_info_to_json(view: ModelInfoView) -> dict[str, Any]:
    """Схема `ModelInfo`."""
    return {
        "model_version_id": view.model_version_id,
        "model_family": view.model_family,
        "feature_schema_version": view.feature_schema_version,
        "embedding_model": view.embedding_model,
        "dataset_version": view.dataset_version,
        "trained_at": _moment(view.trained_at),
        "metrics": dict(view.metrics),
        "feature_names": list(view.feature_names),
    }


def score_to_json(view: ScoreTextView) -> dict[str, Any]:
    """Схема `ScoreResponse`."""
    payload: dict[str, Any] = {
        "score": view.score,
        "decision": view.decision.value,
        "decision_reason": view.decision_reason,
        "features": [
            {
                "feature_name": feature.feature_name,
                "label_ru": feature.label_ru,
                "value": feature.value,
                "contribution": feature.contribution,
                "direction": feature.direction.value,
            }
            for feature in view.features
        ],
        "model_version_id": view.model_version_id,
        "enrichment_applied": view.enrichment_applied,
    }
    if view.predicted_stage is not None:
        payload["predicted_stage"] = view.predicted_stage
    if view.predicted_trend is not None:
        payload["predicted_trend"] = view.predicted_trend
    return payload


def error_to_json(code: str, message: str, request_id: str, retryable: bool = False) -> dict[str, Any]:
    """Схема `Error`: поля `code/message/request_id`, как в нормативном OpenAPI."""
    return {"code": code, "message": message, "request_id": request_id, "retryable": retryable}
