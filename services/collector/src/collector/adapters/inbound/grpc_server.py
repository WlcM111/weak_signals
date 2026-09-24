"""gRPC-сервер `CollectorService`: валидация запросов, вызов use cases, отображение ошибок."""

from __future__ import annotations

from typing import Any

import grpc
from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc

from collector.adapters.inbound.mappers import (
    from_proto_mode,
    from_proto_source_key,
    to_proto_collection,
    to_proto_document,
    to_proto_encyclopedia_hit,
    to_proto_status,
)
from collector.application.dto import StartCollectionCommand
from collector.application.use_cases.cancel_collection import CancelCollection
from collector.application.use_cases.check_encyclopedia import CheckEncyclopedia
from collector.application.use_cases.get_collection import GetCollection
from collector.application.use_cases.get_documents import GetDocuments
from collector.application.use_cases.start_collection import StartCollection
from collector.application.use_cases.stream_documents import StreamDocuments
from collector.application.validation import (
    validate_cancel_reason,
    validate_chunk_size,
    validate_document_ids,
    validate_encyclopedia_request,
    validate_idempotency_key,
    validate_limits,
    validate_mode,
    validate_page_token,
    validate_query_text,
    validate_sources,
    validate_terms,
    validate_uuid,
)
from collector.domain.errors import (
    AppError,
    NotFoundError,
    PreconditionFailedError,
    ResourceExhaustedError,
    UnavailableError,
    ValidationError,
)
from ws_common.logging import get_logger

_STATUS_BY_ERROR: dict[type[AppError], grpc.StatusCode] = {
    ValidationError: grpc.StatusCode.INVALID_ARGUMENT,
    NotFoundError: grpc.StatusCode.NOT_FOUND,
    PreconditionFailedError: grpc.StatusCode.FAILED_PRECONDITION,
    ResourceExhaustedError: grpc.StatusCode.RESOURCE_EXHAUSTED,
    UnavailableError: grpc.StatusCode.UNAVAILABLE,
}


class CollectorServicer(collector_pb2_grpc.CollectorServiceServicer):
    """Транспортный слой: перевод proto ↔ домен и кодов ошибок."""

    def __init__(
        self,
        start_collection: StartCollection,
        get_collection: GetCollection,
        stream_documents: StreamDocuments,
        get_documents: GetDocuments,
        cancel_collection: CancelCollection,
        check_encyclopedia: CheckEncyclopedia,
    ) -> None:
        self._start_collection = start_collection
        self._get_collection = get_collection
        self._stream_documents = stream_documents
        self._get_documents = get_documents
        self._cancel_collection = cancel_collection
        self._check_encyclopedia = check_encyclopedia
        self._log = get_logger("collector.grpc")

    async def StartCollection(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.StartCollectionRequest, context: grpc.aio.ServicerContext
    ) -> collector_pb2.StartCollectionResponse:
        """Идемпотентный запуск сбора."""
        try:
            mode = validate_mode(from_proto_mode(request.mode))
            command = StartCollectionCommand(
                idempotency_key=validate_idempotency_key(request.idempotency_key),
                query_text=validate_query_text(request.query_text),
                terms=validate_terms(request.terms.ru, request.terms.en),
                mode=mode,
                limits=validate_limits(
                    mode,
                    request.limits.max_documents_per_source,
                    request.limits.max_total_documents,
                    request.limits.time_budget_seconds,
                    request.limits.published_since_year,
                ),
                sources=validate_sources([from_proto_source_key(source) for source in request.sources]),
            )
            result = await self._start_collection.execute(command)
        except AppError as error:
            await self._abort(context, error)
        return collector_pb2.StartCollectionResponse(
            collection_id=result.collection_id,
            status=to_proto_status(result.status),
            already_existed=result.already_existed,
        )

    async def GetCollection(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.GetCollectionRequest, context: grpc.aio.ServicerContext
    ) -> collector_pb2.GetCollectionResponse:
        """Статус и прогресс коллекции."""
        try:
            collection_id = validate_uuid(request.collection_id, "collection_id")
            view = await self._get_collection.execute(collection_id)
        except AppError as error:
            await self._abort(context, error)
        return to_proto_collection(view)

    async def StreamDocuments(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.StreamDocumentsRequest, context: grpc.aio.ServicerContext
    ) -> Any:
        """Поток документов завершённой коллекции чанками."""
        try:
            collection_id = validate_uuid(request.collection_id, "collection_id")
            chunk_size = validate_chunk_size(request.chunk_size)
            after = validate_page_token(request.page_token)
            pages = self._stream_documents.execute(collection_id, chunk_size, after)
            async for page in pages:
                yield collector_pb2.DocumentChunk(
                    documents=[to_proto_document(document) for document in page.documents],
                    next_page_token=page.next_page_token,
                    last=page.last,
                )
        except AppError as error:
            await self._abort(context, error)

    async def GetDocuments(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.GetDocumentsRequest, context: grpc.aio.ServicerContext
    ) -> collector_pb2.GetDocumentsResponse:
        """Пакетное чтение документов по идентификаторам."""
        try:
            document_ids = validate_document_ids(list(request.document_ids))
            result = await self._get_documents.execute(document_ids)
        except AppError as error:
            await self._abort(context, error)
        return collector_pb2.GetDocumentsResponse(
            documents=[to_proto_document(document) for document in result.documents],
            missing_document_ids=list(result.missing_document_ids),
        )

    async def CancelCollection(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.CancelCollectionRequest, context: grpc.aio.ServicerContext
    ) -> collector_pb2.CancelCollectionResponse:
        """Кооперативная отмена сбора."""
        try:
            collection_id = validate_uuid(request.collection_id, "collection_id")
            reason = validate_cancel_reason(request.reason)
            status = await self._cancel_collection.execute(collection_id, reason)
        except AppError as error:
            await self._abort(context, error)
        return collector_pb2.CancelCollectionResponse(status=to_proto_status(status))

    async def CheckEncyclopedia(  # noqa: N802 - имя RPC из контракта
        self, request: collector_pb2.CheckEncyclopediaRequest, context: grpc.aio.ServicerContext
    ) -> collector_pb2.CheckEncyclopediaResponse:
        """Индикатор зрелости: наличие статей и просмотры."""
        try:
            titles, language = validate_encyclopedia_request(
                list(request.titles), request.language_code
            )
            hits = await self._check_encyclopedia.execute(titles, language)
        except AppError as error:
            await self._abort(context, error)
        return collector_pb2.CheckEncyclopediaResponse(
            hits=[to_proto_encyclopedia_hit(hit) for hit in hits]
        )

    async def _abort(self, context: grpc.aio.ServicerContext, error: AppError) -> Any:
        """Переводит прикладную ошибку в gRPC-статус и кладёт `error_code` в trailing metadata."""
        code = _STATUS_BY_ERROR.get(type(error), grpc.StatusCode.INTERNAL)
        self._log.warning("rpc.rejected", error_code=error.error_code, code=code.name, message=error.message)
        await context.abort(code, error.message, trailing_metadata=(("error_code", error.error_code),))
