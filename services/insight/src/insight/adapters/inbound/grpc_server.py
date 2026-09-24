"""gRPC-сервер `InsightService` (асинхронный): валидация, вызов сценариев, коды ошибок."""

from __future__ import annotations

import asyncio

import grpc
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

from insight.adapters.inbound import mappers
from insight.application.use_cases.expand_query import ExpandQuery
from insight.application.use_cases.generate_insight import GenerateInsight
from insight.application.use_cases.get_provider_status import GetProviderStatus
from insight.application.validation import validate_query_text
from insight.domain.errors import (
    AppError,
    ResourceExhaustedError,
    UnavailableError,
    ValidationError,
)
from ws_common.logging import get_logger

_STATUS_BY_ERROR: tuple[tuple[type[AppError], grpc.StatusCode], ...] = (
    (ValidationError, grpc.StatusCode.INVALID_ARGUMENT),
    (ResourceExhaustedError, grpc.StatusCode.RESOURCE_EXHAUSTED),
    (UnavailableError, grpc.StatusCode.UNAVAILABLE),
)


class InsightServicer(insight_pb2_grpc.InsightServiceServicer):
    """Транспортный слой: перевод proto ↔ домен и прикладных ошибок в статусы gRPC."""

    def __init__(
        self,
        expand_query: ExpandQuery,
        generate_insight: GenerateInsight,
        provider_status: GetProviderStatus,
        max_queue: int = 100,
    ) -> None:
        self._expand_query = expand_query
        self._generate_insight = generate_insight
        self._provider_status = provider_status
        self._slots = asyncio.Semaphore(max_queue)
        self._max_queue = max_queue
        self._log = get_logger("insight.grpc")

    async def ExpandQuery(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.ExpandQueryRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.ExpandQueryResponse:
        """Расширяет запрос в поисковые фразы; отказ LLM даёт резервное расширение."""
        try:
            query_text = validate_query_text(request.query_text)
            expansion = await self._expand_query.execute(query_text)
        except AppError as error:
            await self._abort(context, error)
        return mappers.to_expand_response(expansion)

    async def GenerateInsight(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.GenerateInsightRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.GenerateInsightResponse:
        """Формирует нарратив кандидата по доказательствам."""
        if self._slots.locked():
            await self._abort(
                context,
                ResourceExhaustedError(
                    f"очередь генерации заполнена (предел {self._max_queue})", "QUEUE_FULL"
                ),
            )
        async with self._slots:
            try:
                command = mappers.from_generate_request(request)
                insight = await self._generate_insight.execute(command)
            except AppError as error:
                await self._abort(context, error)
        return mappers.to_generate_response(insight)

    async def GetProviderStatus(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.GetProviderStatusRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.GetProviderStatusResponse:
        """Состояние провайдеров LLM."""
        states, active = self._provider_status.execute()
        return mappers.to_provider_status_response(states, active)

    async def _abort(self, context: grpc.aio.ServicerContext, error: AppError) -> None:
        """Переводит прикладную ошибку в статус gRPC с `error_code` в trailing metadata."""
        code = grpc.StatusCode.INTERNAL
        for error_type, status_code in _STATUS_BY_ERROR:
            if isinstance(error, error_type):
                code = status_code
                break
        self._log.warning(
            "rpc.rejected", error_code=error.error_code, code=code.name, message=error.message
        )
        await context.abort(code, error.message, (("error_code", error.error_code),))
