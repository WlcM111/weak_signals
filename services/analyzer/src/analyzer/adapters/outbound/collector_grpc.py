"""Синхронный клиент `collector.CollectorService` (§8 HANDOFF, дедлайны — CONTRACT_RULES)."""

from __future__ import annotations

from datetime import UTC

from collections.abc import Iterator, Sequence

import grpc
from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc
from weaksignals.common.v1 import common_pb2

from analyzer.application.dto import CollectionInfo, EncyclopediaHit
from analyzer.domain.entities import DocumentRef
from analyzer.domain.errors import CollectorUnavailable
from analyzer.domain.values import SourceType, TrustLevel
from ws_common.ids import current_correlation_id
from ws_common.logging import get_logger

GET_COLLECTION_DEADLINE = 5.0
STREAM_DEADLINE = 120.0
ENCYCLOPEDIA_DEADLINE = 20.0
START_COLLECTION_DEADLINE = 5.0
STREAM_RESUME_ATTEMPTS = 2


def _md() -> tuple[tuple[str, str], ...]:
    """Метаданные исходящего вызова: сквозной correlation id и имя вызывающего сервиса (§10.2 ТЗ)."""
    return (("x-correlation-id", current_correlation_id()), ("x-caller", "analyzer"))

TERMINAL_STATUSES = frozenset(
    {
        common_pb2.OPERATION_STATUS_COMPLETED,
        common_pb2.OPERATION_STATUS_PARTIAL,
        common_pb2.OPERATION_STATUS_FAILED,
        common_pb2.OPERATION_STATUS_CANCELLED,
    }
)


class CollectorGrpcClient:
    """Реализация порта `CollectorReader` поверх gRPC-канала."""

    def __init__(self, channel: grpc.Channel) -> None:
        self._stub = collector_pb2_grpc.CollectorServiceStub(channel)
        self._log = get_logger("analyzer.collector_client")

    def get_collection(self, collection_id: str) -> CollectionInfo:
        """Статус и объём коллекции."""
        try:
            response = self._stub.GetCollection(
                collector_pb2.GetCollectionRequest(collection_id=collection_id),
                timeout=GET_COLLECTION_DEADLINE, metadata=_md(),
            )
        except grpc.RpcError as error:
            raise _unavailable("GetCollection", error) from error
        return CollectionInfo(
            collection_id=response.collection_id,
            status=common_pb2.OperationStatus.Name(response.status).removeprefix("OPERATION_STATUS_"),
            documents_total=response.documents_total,
            is_terminal=response.status in TERMINAL_STATUSES,
        )

    def stream_documents(self, collection_id: str, chunk_size: int, limit: int) -> Iterator[DocumentRef]:
        """Документы коллекции потоком с возобновлением по `page_token` при обрыве."""
        page_token = ""
        produced = 0
        attempts = 0
        while produced < limit:
            try:
                stream = self._stub.StreamDocuments(
                    collector_pb2.StreamDocumentsRequest(
                        collection_id=collection_id, chunk_size=chunk_size, page_token=page_token
                    ),
                    timeout=STREAM_DEADLINE, metadata=_md(),
                )
                last_token = page_token
                for chunk in stream:
                    for document in chunk.documents:
                        yield _to_document_ref(document, produced)
                        produced += 1
                        if produced >= limit:
                            return
                    last_token = chunk.next_page_token
                    if chunk.last:
                        return
                page_token = last_token
                attempts = 0
            except grpc.RpcError as error:
                attempts += 1
                if attempts > STREAM_RESUME_ATTEMPTS or not _is_retryable(error):
                    raise _unavailable("StreamDocuments", error) from error
                self._log.warning(
                    "collector.stream_resumed", attempt=attempts, code=str(error.code())
                )

    def check_encyclopedia(self, titles: Sequence[str], language_code: str) -> list[EncyclopediaHit]:
        """Проверка статей Wikipedia пачкой до 20 названий."""
        try:
            response = self._stub.CheckEncyclopedia(
                collector_pb2.CheckEncyclopediaRequest(
                    titles=list(titles), language_code=language_code
                ),
                timeout=ENCYCLOPEDIA_DEADLINE, metadata=_md(),
            )
        except grpc.RpcError as error:
            raise _unavailable("CheckEncyclopedia", error) from error
        return [
            EncyclopediaHit(
                title=hit.title,
                exists=hit.exists,
                page_url=hit.page_url,
                pageviews_30d=hit.pageviews_30d,
                created_at=hit.created_at.ToDatetime(tzinfo=UTC) if hit.HasField("created_at") else None,
            )
            for hit in response.hits
        ]

    def start_enrichment(self, idempotency_key: str, title: str) -> str:
        """Запускает ENRICHMENT-сбор по названию технологии."""
        request = collector_pb2.StartCollectionRequest(
            idempotency_key=idempotency_key,
            query_text=title,
            terms=collector_pb2.SearchTerms(ru=[title], en=[title]),
            mode=collector_pb2.COLLECTION_MODE_ENRICHMENT,
        )
        try:
            response = self._stub.StartCollection(request, timeout=START_COLLECTION_DEADLINE, metadata=_md())
        except grpc.RpcError as error:
            raise _unavailable("StartCollection", error) from error
        return response.collection_id


def _to_document_ref(document: common_pb2.Document, relevance_rank: int) -> DocumentRef:
    """`common.v1.Document` → документ в памяти анализа."""
    return DocumentRef(
        document_id=document.document_id,
        title=document.title,
        text=document.text,
        language_code=document.language_code,
        source_type=SourceType[
            common_pb2.SourceType.Name(document.source_type).removeprefix("SOURCE_TYPE_")
        ],
        trust_level=TrustLevel[
            common_pb2.TrustLevel.Name(document.trust_level).removeprefix("TRUST_LEVEL_")
        ],
        origin_domain=document.origin_domain,
        url=document.url,
        published_at=document.published_at.ToDatetime(tzinfo=UTC) if document.HasField("published_at") else None,
        citation_count=document.citation_count if document.HasField("citation_count") else None,
        engagement_count=document.engagement_count if document.HasField("engagement_count") else None,
        relevance_rank=relevance_rank,
    )


def _is_retryable(error: grpc.RpcError) -> bool:
    """Повторяем только обрывы и недоступность (§10.3 ТЗ)."""
    return error.code() in {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.INTERNAL,
    }


def _unavailable(rpc: str, error: grpc.RpcError) -> CollectorUnavailable:
    """Переводит отказ клиента в доменную ошибку с понятным сообщением."""
    return CollectorUnavailable(f"collector.{rpc}: {error.code().name} {error.details()}")
