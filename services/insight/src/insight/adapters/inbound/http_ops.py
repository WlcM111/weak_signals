"""HTTP-эндпоинты обслуживания insight: `/healthz`, `/readyz`, `/metrics` (§9 COMMON)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ws_common.logging import get_logger
from ws_common.metrics import render_metrics


class OpsHttpServer:
    """Минимальный сервер состояния на asyncio: три статических маршрута без фреймворка."""

    def __init__(
        self,
        port: int,
        readiness: Callable[[], bool],
        host: str = "0.0.0.0",  # noqa: S104 - внутренняя сеть compose
    ) -> None:
        self._port = port
        self._host = host
        self._readiness = readiness
        self._server: asyncio.AbstractServer | None = None
        self._log = get_logger("insight.ops")

    async def start(self) -> None:
        """Поднимает сервер."""
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        self._log.info("ops.started", port=self._port)

    async def stop(self) -> None:
        """Останавливает сервер."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Обрабатывает один запрос."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            path = request_line.decode("latin-1").split(" ")[1] if b" " in request_line else "/"
            body, status, content_type = self._route(path.split("?")[0])
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, IndexError):
            return
        finally:
            writer.close()

    def _route(self, path: str) -> tuple[bytes, str, str]:
        """Маршрутизация трёх эндпоинтов обслуживания."""
        if path == "/healthz":
            return b'{"status":"ok"}', "200 OK", "application/json"
        if path == "/readyz":
            ready = self._readiness()
            return (
                (b'{"status":"ready"}', "200 OK", "application/json")
                if ready
                else (b'{"status":"not-ready"}', "503 Service Unavailable", "application/json")
            )
        if path == "/metrics":
            return render_metrics(), "200 OK", "text/plain; version=0.0.4"
        return b"not found", "404 Not Found", "text/plain"
