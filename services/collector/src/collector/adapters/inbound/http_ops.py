"""HTTP-эндпоинты обслуживания: `/healthz`, `/readyz`, `/metrics` (§9 COMMON).

Реализован на asyncio без веб-фреймворка: три статических маршрута не требуют зависимости
уровня FastAPI в сервисе, где основной транспорт — gRPC.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from ws_common.logging import get_logger

MAX_REQUEST_BYTES = 8192
_READ_TIMEOUT_SECONDS = 5.0


class OpsHttpServer:
    """Сервер состояния и метрик сервиса."""

    def __init__(
        self,
        port: int,
        readiness: Callable[[], Awaitable[bool]],
        metrics: Callable[[], bytes],
        host: str = "0.0.0.0",  # noqa: S104 - внутренняя сеть compose, порт не публикуется
    ) -> None:
        self._port = port
        self._host = host
        self._readiness = readiness
        self._metrics = metrics
        self._server: asyncio.AbstractServer | None = None
        self._log = get_logger("collector.ops")

    async def start(self) -> None:
        """Запускает сервер."""
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        self._log.info("ops.started", port=self._port)

    async def stop(self) -> None:
        """Останавливает сервер и дожидается закрытия соединений."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Обрабатывает один запрос и закрывает соединение."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_SECONDS)
            if len(request_line) > MAX_REQUEST_BYTES:
                await self._respond(writer, 431, b"request line too long")
                return
            parts = request_line.decode("latin-1").split()
            method, path = (parts[0], parts[1].split("?")[0]) if len(parts) >= 2 else ("", "")
            while True:  # заголовки не используются, но должны быть вычитаны
                line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_SECONDS)
                if line in (b"\r\n", b"\n", b""):
                    break
            await self._route(writer, method, path)
        except (TimeoutError, ConnectionError, UnicodeDecodeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _route(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        """Маршрутизация трёх эндпоинтов обслуживания."""
        if method != "GET":
            await self._respond(writer, 405, b"method not allowed")
        elif path == "/healthz":
            await self._respond(writer, 200, b'{"status":"ok"}', "application/json")
        elif path == "/readyz":
            ready = await self._readiness()
            await self._respond(
                writer,
                200 if ready else 503,
                b'{"status":"ready"}' if ready else b'{"status":"not-ready"}',
                "application/json",
            )
        elif path == "/metrics":
            await self._respond(writer, 200, self._metrics(), "text/plain; version=0.0.4")
        else:
            await self._respond(writer, 404, b"not found")

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str = "text/plain"
    ) -> None:
        """Отправляет ответ с обязательными заголовками."""
        head = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'ERROR'}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()
