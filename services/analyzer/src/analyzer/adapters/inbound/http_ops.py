"""HTTP-эндпоинты обслуживания analyzer: `/healthz`, `/readyz`, `/metrics` (§9 COMMON).

Синхронный сервер на `http.server` в отдельном потоке: сервис использует sync-стек (ADR-09),
а трёх статических маршрутов недостаточно, чтобы вводить веб-фреймворк.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ws_common.logging import get_logger


class OpsHttpServer:
    """Сервер состояния и метрик сервиса."""

    def __init__(
        self,
        port: int,
        readiness: Callable[[], bool],
        metrics: Callable[[], bytes],
        host: str = "0.0.0.0",  # noqa: S104 - внутренняя сеть compose, порт не публикуется
    ) -> None:
        self._log = get_logger("analyzer.ops")
        handler = _build_handler(readiness, metrics)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="ops-http", daemon=True)
        self._port = port

    def start(self) -> None:
        """Запускает сервер в фоновом потоке."""
        self._thread.start()
        self._log.info("ops.started", port=self._port)

    def stop(self) -> None:
        """Останавливает сервер и дожидается завершения потока."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _build_handler(
    readiness: Callable[[], bool], metrics: Callable[[], bytes]
) -> type[BaseHTTPRequestHandler]:
    """Создаёт обработчик с замкнутыми проверками готовности и метриками."""

    class Handler(BaseHTTPRequestHandler):
        """Обработчик трёх маршрутов обслуживания."""

        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - имя метода задано BaseHTTPRequestHandler
            """Маршрутизация GET-запросов."""
            path = self.path.split("?")[0]
            if path == "/healthz":
                self._respond(200, b'{"status":"ok"}', "application/json")
            elif path == "/readyz":
                ready = readiness()
                self._respond(
                    200 if ready else 503,
                    b'{"status":"ready"}' if ready else b'{"status":"not-ready"}',
                    "application/json",
                )
            elif path == "/metrics":
                self._respond(200, metrics(), "text/plain; version=0.0.4")
            else:
                self._respond(404, b"not found")

        def _respond(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            """Отправляет ответ с обязательными заголовками."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - сигнатура базового класса
            """Отключает вывод стандартного лога: события пишет structlog."""

    return Handler
