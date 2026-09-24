"""Серверные интерсепторы gRPC: correlation id, логирование, метрики (§9, §10.1 ТЗ)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
import structlog

from ws_common.ids import new_correlation_id
from ws_common.metrics import GRPC_LATENCY, GRPC_REQUESTS

CORRELATION_ID_KEY = "x-correlation-id"
CALLER_KEY = "x-caller"
HEALTH_SERVICE_PREFIX = "/grpc.health.v1.Health/"
_MAX_CORRELATION_ID_LEN = 64


def _metadata_value(metadata: tuple[tuple[str, str | bytes], ...] | None, key: str) -> str:
    """Значение метаданных по ключу (пусто, если отсутствует или бинарное)."""
    for name, value in metadata or ():
        if name.lower() == key and isinstance(value, str):
            return value
    return ""


class ObservabilityInterceptor(grpc.aio.ServerInterceptor):
    """Проставляет correlation id, пишет событие `rpc.finished` и метрики по каждому вызову."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._log = structlog.get_logger("grpc")

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = await continuation(handler_call_details)
        method = handler_call_details.method or ""
        rpc = method.rsplit("/", 1)[-1]
        quiet = HEALTH_SERVICE_PREFIX in method  # проверки здоровья compose: раз в 10 с, без шума в логах
        metadata = tuple(handler_call_details.invocation_metadata or ())
        correlation_id = _metadata_value(metadata, CORRELATION_ID_KEY)[:_MAX_CORRELATION_ID_LEN]
        caller = _metadata_value(metadata, CALLER_KEY)
        if not correlation_id:
            correlation_id = new_correlation_id()
            if not quiet:
                self._log.warning("rpc.correlation_id_missing", rpc=rpc, caller=caller)
        return self._wrap(handler, rpc, correlation_id, caller, quiet)

    def _wrap(
        self, handler: grpc.RpcMethodHandler, rpc: str, correlation_id: str, caller: str, quiet: bool = False
    ) -> grpc.RpcMethodHandler:
        """Оборачивает унарный и стримовый обработчики одной логикой наблюдаемости."""

        def observe(inner: Callable[..., Any], streaming: bool) -> Callable[..., Any]:
            async def unary(request: Any, context: grpc.aio.ServicerContext) -> Any:
                structlog.contextvars.bind_contextvars(correlation_id=correlation_id, rpc=rpc)
                started = time.perf_counter()
                try:
                    result = await inner(request, context)
                except grpc.aio.AioRpcError as exc:  # ошибка апстрима внутри обработчика
                    self._finish(rpc, exc.code(), started, caller, quiet)
                    raise
                except Exception:  # context.abort() и непредвиденные ошибки: вызов тоже должен быть учтён
                    self._finish(rpc, context.code() or grpc.StatusCode.UNKNOWN, started, caller, quiet)
                    raise
                else:
                    self._finish(rpc, context.code() or grpc.StatusCode.OK, started, caller, quiet)
                    return result
                finally:
                    structlog.contextvars.unbind_contextvars("correlation_id", "rpc")

            async def stream(request: Any, context: grpc.aio.ServicerContext) -> Any:
                structlog.contextvars.bind_contextvars(correlation_id=correlation_id, rpc=rpc)
                started = time.perf_counter()
                try:
                    async for item in inner(request, context):
                        yield item
                    self._finish(rpc, context.code() or grpc.StatusCode.OK, started, caller, quiet)
                except Exception:  # отклонённый или оборванный поток учитывается с фактическим кодом
                    self._finish(rpc, context.code() or grpc.StatusCode.UNKNOWN, started, caller, quiet)
                    raise
                finally:
                    structlog.contextvars.unbind_contextvars("correlation_id", "rpc")

            return stream if streaming else unary

        if handler.unary_unary is not None:
            return grpc.unary_unary_rpc_method_handler(
                observe(handler.unary_unary, streaming=False),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.unary_stream is not None:
            return grpc.unary_stream_rpc_method_handler(
                observe(handler.unary_stream, streaming=True),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler

    def _finish(
        self, rpc: str, code: grpc.StatusCode, started: float, caller: str, quiet: bool = False
    ) -> None:
        """Единая запись метрик и лога завершения вызова."""
        duration = time.perf_counter() - started
        GRPC_REQUESTS.labels(self._service, rpc, code.name).inc()
        GRPC_LATENCY.labels(self._service, rpc).observe(duration)
        emit = self._log.debug if quiet else self._log.info
        emit("rpc.finished", rpc=rpc, code=code.name, duration_ms=round(duration * 1000, 2), caller=caller)


class SyncObservabilityInterceptor(grpc.ServerInterceptor):
    """Серверный интерсептор для синхронного gRPC (analyzer): correlation id, лог, метрики."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._log = structlog.get_logger("grpc")

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = continuation(handler_call_details)
        if handler is None or not handler.unary_unary:
            return handler
        rpc = (handler_call_details.method or "").rsplit("/", 1)[-1]
        metadata = tuple(handler_call_details.invocation_metadata or ())
        correlation_id = (
            _metadata_value(metadata, CORRELATION_ID_KEY)[:_MAX_CORRELATION_ID_LEN] or new_correlation_id()
        )
        caller = _metadata_value(metadata, CALLER_KEY)
        inner = handler.unary_unary

        def wrapper(request: Any, context: grpc.ServicerContext) -> Any:
            structlog.contextvars.bind_contextvars(correlation_id=correlation_id, rpc=rpc)
            started = time.perf_counter()
            code = grpc.StatusCode.OK
            try:
                return inner(request, context)
            except grpc.RpcError:
                code = context.code() or grpc.StatusCode.UNKNOWN
                raise
            finally:
                duration = time.perf_counter() - started
                code = context.code() or code
                GRPC_REQUESTS.labels(self._service, rpc, code.name).inc()
                GRPC_LATENCY.labels(self._service, rpc).observe(duration)
                quiet = HEALTH_SERVICE_PREFIX in (handler_call_details.method or "")
                emit = self._log.debug if quiet else self._log.info
                emit(
                    "rpc.finished",
                    rpc=rpc,
                    code=code.name,
                    duration_ms=round(duration * 1000, 2),
                    caller=caller,
                )
                structlog.contextvars.unbind_contextvars("correlation_id", "rpc")

        return grpc.unary_unary_rpc_method_handler(
            wrapper,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
