"""Построение gRPC-каналов к внутренним сервисам с едиными метаданными и дедлайнами."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import grpc

from ws_common.grpc_interceptors import CALLER_KEY, CORRELATION_ID_KEY
from ws_common.ids import new_correlation_id


class CorrelationClientInterceptor(
    grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor
):
    """Добавляет `x-correlation-id` и `x-caller` в каждый исходящий вызов (§10.2 ТЗ)."""

    def __init__(self, caller: str) -> None:
        self._caller = caller

    def _with_metadata(self, details: grpc.ClientCallDetails) -> grpc.ClientCallDetails:
        """Копия параметров вызова с добавленными метаданными."""
        metadata = list(details.metadata or [])
        keys = {key for key, _ in metadata}
        if CORRELATION_ID_KEY not in keys:
            metadata.append((CORRELATION_ID_KEY, new_correlation_id()))
        if CALLER_KEY not in keys:
            metadata.append((CALLER_KEY, self._caller))
        return _CallDetails(
            method=details.method,
            timeout=details.timeout,
            metadata=metadata,
            credentials=details.credentials,
            wait_for_ready=details.wait_for_ready,
            compression=details.compression,
        )

    def intercept_unary_unary(
        self, continuation: Callable[..., Any], client_call_details: grpc.ClientCallDetails, request: Any
    ) -> Any:
        return continuation(self._with_metadata(client_call_details), request)

    def intercept_unary_stream(
        self, continuation: Callable[..., Any], client_call_details: grpc.ClientCallDetails, request: Any
    ) -> Iterator[Any]:
        return continuation(self._with_metadata(client_call_details), request)


class _CallDetails(grpc.ClientCallDetails):
    """Простая реализация параметров вызова с изменёнными метаданными."""

    def __init__(
        self,
        method: str,
        timeout: float | None,
        metadata: list[tuple[str, str]],
        credentials: Any,
        wait_for_ready: bool | None,
        compression: Any,
    ) -> None:
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials
        self.wait_for_ready = wait_for_ready
        self.compression = compression


def build_channel(target: str, caller: str, max_message_mb: int = 16) -> grpc.Channel:
    """Канал к внутреннему сервису с интерсептором метаданных и лимитами сообщений."""
    channel = grpc.insecure_channel(
        target,
        options=[
            ("grpc.max_receive_message_length", max_message_mb * 1024 * 1024),
            ("grpc.max_send_message_length", max_message_mb * 1024 * 1024),
            ("grpc.keepalive_time_ms", 30000),
        ],
    )
    return grpc.intercept_channel(channel, CorrelationClientInterceptor(caller))
