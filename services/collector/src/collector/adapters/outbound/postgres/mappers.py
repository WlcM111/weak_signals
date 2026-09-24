"""Преобразование строк PostgreSQL в доменные объекты collector."""

from __future__ import annotations

from typing import Any

from collector.domain.entities import AdapterRun, Collection, Document
from collector.domain.values import (
    CollectionLimits,
    CollectionMode,
    Lease,
    OperationStatus,
    SearchTerms,
    SourceKey,
    SourceType,
    TrustLevel,
)

COLLECTION_COLUMNS = (
    "collection_id, idempotency_key, query_text, mode, terms_ru, terms_en, max_documents_per_source, "
    "max_total_documents, time_budget_seconds, published_since_year, status, cancel_requested, "
    "lease_owner, lease_expires_at, documents_total, http_requests_total, error_code, error_message, "
    "created_at, started_at, finished_at"
)

DOCUMENT_COLUMNS = (
    "document_id, url, title, body_text, language_code, published_at, source_key, source_type, "
    "trust_level, doi, citation_count, engagement_count, fetched_at, origin_domain"
)

def qualify(columns: str, alias: str) -> str:
    """Список колонок с псевдонимом таблицы: нужен, когда в запросе две таблицы с одноимёнными колонками."""
    return ", ".join(f"{alias}.{name.strip()}" for name in columns.split(","))


ADAPTER_RUN_COLUMNS = (
    "source_key, status, http_requests, documents_found, documents_new, error_code, error_message, "
    "started_at, finished_at"
)


def row_to_collection(row: dict[str, Any]) -> Collection:
    """Строка `collections` → сущность коллекции."""
    lease = (
        Lease(owner=row["lease_owner"], expires_at=row["lease_expires_at"])
        if row["lease_owner"] and row["lease_expires_at"]
        else None
    )
    return Collection(
        collection_id=str(row["collection_id"]),
        idempotency_key=row["idempotency_key"],
        query_text=row["query_text"],
        mode=CollectionMode(row["mode"]),
        terms=SearchTerms(ru=tuple(row["terms_ru"] or ()), en=tuple(row["terms_en"] or ())),
        limits=CollectionLimits(
            max_documents_per_source=row["max_documents_per_source"],
            max_total_documents=row["max_total_documents"],
            time_budget_seconds=row["time_budget_seconds"],
            published_since_year=row["published_since_year"],
        ),
        status=OperationStatus(row["status"]),
        cancel_requested=row["cancel_requested"],
        lease=lease,
        documents_total=row["documents_total"],
        http_requests_total=row["http_requests_total"],
        error_code=row["error_code"] or "",
        error_message=row["error_message"] or "",
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def row_to_document(row: dict[str, Any]) -> Document:
    """Строка `documents` → документ, отдаваемый наружу."""
    return Document(
        document_id=str(row["document_id"]),
        url=row["url"],
        title=row["title"],
        text=row["body_text"],
        language_code=row["language_code"],
        source_key=SourceKey(row["source_key"]),
        source_type=SourceType(row["source_type"]),
        trust_level=TrustLevel(row["trust_level"]),
        fetched_at=row["fetched_at"],
        origin_domain=row["origin_domain"],
        published_at=row["published_at"],
        doi=row["doi"],
        citation_count=row["citation_count"],
        engagement_count=row["engagement_count"],
    )


def row_to_adapter_run(row: dict[str, Any]) -> AdapterRun:
    """Строка `adapter_runs` → запуск адаптера."""
    return AdapterRun(
        source_key=SourceKey(row["source_key"]),
        status=OperationStatus(row["status"]),
        http_requests=row["http_requests"],
        documents_found=row["documents_found"],
        documents_new=row["documents_new"],
        error_code=row["error_code"] or "",
        error_message=row["error_message"] or "",
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
