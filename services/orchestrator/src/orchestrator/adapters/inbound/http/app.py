"""Сборка приложения FastAPI: маршруты — тонкие обёртки над `ApiHandlers` (§8 HANDOFF)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request as FastApiRequest, Response as FastApiResponse
from fastapi.responses import JSONResponse

from orchestrator.adapters.inbound.http import presenters, schemas
from orchestrator.adapters.inbound.http.handlers import ApiHandlers, Request
from ws_common.ids import new_correlation_id, set_correlation_id
from ws_common.logging import get_logger
from ws_common.metrics import HTTP_REQUESTS, render_metrics

CORRELATION_HEADER = "x-correlation-id"
API_KEY_HEADER = "x-api-key"
IDEMPOTENCY_HEADER = "idempotency-key"
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
log = get_logger("orchestrator.api")


def build_request(request: FastApiRequest, body: dict[str, Any] | None = None) -> Request:
    """Переводит запрос FastAPI в представление, с которым работают обработчики."""
    raw = request.headers
    return Request(
        api_key=raw.get(API_KEY_HEADER),
        idempotency_key=raw.get(IDEMPOTENCY_HEADER),
        correlation_id=raw.get(CORRELATION_HEADER) or new_correlation_id(),
        client_ip=request.client.host if request.client else None,
        body=body,
        body_size=int(raw.get("content-length") or 0),
        query=dict(request.query_params),
    )


def create_app(handlers: ApiHandlers, lifespan: Any = None) -> FastAPI:
    """Создаёт приложение со всеми путями нормативного OpenAPI."""
    app = FastAPI(
        title="Weak Signals Orchestrator API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def observe(request: FastApiRequest, call_next: Any) -> FastApiResponse:
        """Лог, метрика и correlation id каждого запроса; непредвиденное исключение → JSON-ошибка контракта.

        Без этого слоя любое исключение вне `AppError` (сбой БД, ошибка сериализации) отдавалось
        клиенту голым текстом `Internal Server Error`, который интерфейс не может разобрать.
        """
        started = time.perf_counter()
        correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
        set_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - последний рубеж: клиент всегда получает JSON по контракту
            log.error(
                "api.unhandled",
                path=request.url.path,
                error=f"{type(error).__name__}: {error}"[:500],
                correlation_id=correlation_id,
            )
            response = JSONResponse(
                status_code=500,
                content=presenters.error_to_json(
                    "INTERNAL_ERROR", "внутренняя ошибка сервиса", correlation_id, retryable=False
                ),
            )
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(path, str(response.status_code)).inc()
        if path not in QUIET_PATHS:
            log.info(
                "api.access",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                correlation_id=correlation_id,
            )
        return response

    def respond(result: Any) -> FastApiResponse:
        """Ответ обработчика → ответ FastAPI."""
        return JSONResponse(status_code=result.status, content=result.body, headers=result.headers)

    @app.post("/api/v1/queries", status_code=202, response_model=schemas.JobAccepted)
    async def create_query(payload: schemas.CreateQueryRequest, request: FastApiRequest) -> Any:
        """Принимает запрос пользователя и ставит задание в очередь."""
        return respond(await handlers.create_query(build_request(request, payload.model_dump())))

    @app.get("/api/v1/jobs", response_model=schemas.JobList)
    async def list_jobs(request: FastApiRequest) -> Any:
        """Список заданий."""
        return respond(await handlers.list_jobs(build_request(request)))

    @app.get("/api/v1/jobs/{job_id}", response_model=schemas.JobSchema)
    async def get_job(job_id: str, request: FastApiRequest) -> Any:
        """Состояние задания."""
        return respond(await handlers.get_job(job_id, build_request(request)))

    @app.post("/api/v1/jobs/{job_id}/cancel", status_code=202, response_model=schemas.JobSchema)
    async def cancel_job(job_id: str, request: FastApiRequest) -> Any:
        """Отмена задания."""
        return respond(await handlers.cancel_job(job_id, build_request(request)))

    @app.get("/api/v1/jobs/{job_id}/results", response_model=schemas.ResultsSchema)
    async def get_results(job_id: str, request: FastApiRequest) -> Any:
        """Снимок результата задания."""
        return respond(await handlers.get_results(job_id, build_request(request)))

    @app.get("/api/v1/results/items/{item_id}", response_model=schemas.ResultItemSchema)
    async def get_result_item(item_id: str, request: FastApiRequest) -> Any:
        """Элемент выдачи целиком."""
        return respond(await handlers.get_result_item(item_id, build_request(request)))

    @app.get("/api/v1/model", response_model=schemas.ModelInfoSchema)
    async def get_model(request: FastApiRequest) -> Any:
        """Сведения об активной модели."""
        return respond(await handlers.get_model(build_request(request)))

    @app.post("/api/v1/score", response_model=schemas.ScoreResponse)
    async def score(payload: schemas.ScoreRequest, request: FastApiRequest) -> Any:
        """Прямой скоринг описания технологии."""
        return respond(await handlers.score(build_request(request, payload.model_dump())))

    @app.get("/healthz", response_model=schemas.HealthSchema)
    async def healthz() -> Any:
        """Процесс жив."""
        return respond(await handlers.healthz())

    @app.get("/readyz", response_model=schemas.ReadinessSchema)
    async def readyz() -> Any:
        """Готовность зависимостей."""
        return respond(await handlers.readyz())

    @app.get("/metrics")
    async def metrics() -> FastApiResponse:
        """Метрики Prometheus."""
        return FastApiResponse(content=render_metrics(), media_type="text/plain; version=0.0.4")

    return app
