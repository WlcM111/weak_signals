"""Преобразование доменных объектов в сообщения protobuf и обратно (контракты 1.0.0).

Перечисления домена (`StrEnum` без префикса) и proto (с префиксом) связаны по имени члена:
`SourceKey.OPENALEX` ↔ `SOURCE_KEY_OPENALEX`. Совпадение проверяется `tools/check_proto_conformance.py`.
"""

from __future__ import annotations

from datetime import datetime

from google.protobuf.timestamp_pb2 import Timestamp
from weaksignals.collector.v1 import collector_pb2
from weaksignals.common.v1 import common_pb2

from collector.application.dto import CollectionView, EncyclopediaHit
from collector.domain.entities import AdapterRun, Document
from collector.domain.values import (
    CollectionMode,
    OperationStatus,
    SourceKey,
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


def to_proto_source_key(source_key: SourceKey) -> int:
    """Ключ источника → `common.v1.SourceKey`."""
    return int(common_pb2.SourceKey.Value(f"SOURCE_KEY_{source_key.name}"))


def to_proto_source_type(source_type: SourceType) -> int:
    """Тип источника → `common.v1.SourceType`."""
    return int(common_pb2.SourceType.Value(f"SOURCE_TYPE_{source_type.name}"))


def to_proto_trust_level(trust_level: TrustLevel) -> int:
    """Уровень доверенности → `common.v1.TrustLevel`."""
    return int(common_pb2.TrustLevel.Value(f"TRUST_LEVEL_{trust_level.name}"))


def from_proto_source_key(value: int) -> SourceKey | None:
    """`common.v1.SourceKey` → домен; None для UNSPECIFIED и неизвестных значений."""
    name = common_pb2.SourceKey.Name(value) if value in common_pb2.SourceKey.values() else ""
    if not name or name == "SOURCE_KEY_UNSPECIFIED":
        return None
    return SourceKey[name.removeprefix("SOURCE_KEY_")]


def from_proto_mode(value: int) -> CollectionMode | None:
    """`collector.v1.CollectionMode` → домен; None для UNSPECIFIED."""
    name = collector_pb2.CollectionMode.Name(value) if value in collector_pb2.CollectionMode.values() else ""
    if not name or name == "COLLECTION_MODE_UNSPECIFIED":
        return None
    return CollectionMode[name.removeprefix("COLLECTION_MODE_")]


def to_proto_document(document: Document) -> common_pb2.Document:
    """Документ домена → `common.v1.Document` (необязательные поля не выставляются, если их нет)."""
    message = common_pb2.Document(
        document_id=document.document_id,
        url=document.url,
        title=document.title,
        text=document.text,
        language_code=document.language_code,
        source_key=to_proto_source_key(document.source_key),
        source_type=to_proto_source_type(document.source_type),
        trust_level=to_proto_trust_level(document.trust_level),
        fetched_at=to_timestamp(document.fetched_at),
        origin_domain=document.origin_domain,
    )
    if document.published_at is not None:
        message.published_at.CopyFrom(to_timestamp(document.published_at))
    if document.doi is not None:
        message.doi = document.doi
    if document.citation_count is not None:
        message.citation_count = document.citation_count
    if document.engagement_count is not None:
        message.engagement_count = document.engagement_count
    return message


def to_proto_adapter_run(run: AdapterRun) -> collector_pb2.AdapterRunSummary:
    """Запуск адаптера → `AdapterRunSummary`."""
    return collector_pb2.AdapterRunSummary(
        source_key=to_proto_source_key(run.source_key),
        status=to_proto_status(run.status),
        http_requests=run.http_requests,
        documents_found=run.documents_found,
        documents_new=run.documents_new,
        error_code=run.error_code,
        error_message=run.error_message,
    )


def to_proto_collection(view: CollectionView) -> collector_pb2.GetCollectionResponse:
    """Состояние коллекции → `GetCollectionResponse`."""
    response = collector_pb2.GetCollectionResponse(
        collection_id=view.collection_id,
        status=to_proto_status(view.status),
        documents_total=view.documents_total,
        sources_processed=view.sources_processed,
        http_requests_total=view.http_requests_total,
        adapter_runs=[to_proto_adapter_run(run) for run in view.adapter_runs],
        error_code=view.error_code,
        error_message=view.error_message,
    )
    if view.started_at is not None:
        response.started_at.CopyFrom(to_timestamp(view.started_at))
    if view.finished_at is not None:
        response.finished_at.CopyFrom(to_timestamp(view.finished_at))
    return response


def to_proto_encyclopedia_hit(hit: EncyclopediaHit) -> collector_pb2.EncyclopediaHit:
    """Результат проверки статьи → `EncyclopediaHit`."""
    message = collector_pb2.EncyclopediaHit(
        title=hit.title,
        exists=hit.exists,
        page_url=hit.page_url,
        pageviews_30d=hit.pageviews_30d,
    )
    if hit.created_at is not None:
        message.created_at.CopyFrom(to_timestamp(hit.created_at))
    return message
