"""Обработчики HTTP-путей без привязки к веб-фреймворку.

Каждый метод возвращает `(статус, тело)` по `orchestrator.openapi.yaml`. FastAPI-роутеры —
тонкие обёртки над этим классом, поэтому поведение API (валидация, идемпотентность, коды
ошибок, пагинация) проверяется тестами без поднятия сервера.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from orchestrator.adapters.inbound.http import presenters
from orchestrator.adapters.inbound.http.deps import ApiKeyGuard, RateLimiter, check_body_size
from orchestrator.application.dto import SubmitQueryCommand
from orchestrator.application.use_cases.cancel_job import CancelJob
from orchestrator.application.use_cases.get_job import GetJob, ListJobs
from orchestrator.application.use_cases.get_results import GetResultItem, GetResults
from orchestrator.application.use_cases.proxy import GetModelInfo, ScoreText
from orchestrator.application.use_cases.submit_query import SubmitQuery
from orchestrator.application.validation import (
    canonical_request_hash,
    decode_cursor,
    hash_client_ip,
    validate_idempotency_key,
    validate_page_size,
    validate_query_text,
    validate_score_request,
    validate_status_filter,
    validate_top_n,
    validate_uuid,
)
from orchestrator.domain.errors import (
    AppError,
    IdempotencyConflict,
    JobNotCancellable,
    NotFoundError,
    PreconditionFailedError,
    QueueFull,
    RateLimited,
    Unauthorized,
    UnavailableError,
    ValidationError,
)
from ws_common.logging import get_logger

_STATUS_BY_ERROR: tuple[tuple[type[AppError], int], ...] = (
    (ValidationError, 400),
    (Unauthorized, 401),
    (NotFoundError, 404),
    (IdempotencyConflict, 409),
    (JobNotCancellable, 409),
    (PreconditionFailedError, 409),
    (QueueFull, 429),
    (RateLimited, 429),
    (UnavailableError, 502),
)


@dataclass(frozen=True, slots=True)
class Request:
    """Минимальное представление HTTP-запроса, нужное обработчикам."""

    api_key: str | None = None
    idempotency_key: str | None = None
    correlation_id: str = ""
    client_ip: str | None = None
    body: dict[str, Any] | None = None
    body_size: int = 0
    query: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Response:
    """Ответ обработчика: статус, тело и дополнительные заголовки."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] | None = None


class ApiHandlers:
    """Все пути `/api/v1/*` и эндпоинты обслуживания."""

    def __init__(
        self,
        *,
        submit_query: SubmitQuery,
        get_job: GetJob,
        list_jobs: ListJobs,
        cancel_job: CancelJob,
        get_results: GetResults,
        get_result_item: GetResultItem,
        get_model_info: GetModelInfo,
        score_text: ScoreText,
        guard: ApiKeyGuard,
        rate_limiter: RateLimiter,
        ip_salt: str = "",
        readiness: Any = None,
    ) -> None:
        self._submit_query = submit_query
        self._get_job = get_job
        self._list_jobs = list_jobs
        self._cancel_job = cancel_job
        self._get_results = get_results
        self._get_result_item = get_result_item
        self._get_model_info = get_model_info
        self._score_text = score_text
        self._guard = guard
        self._rate_limiter = rate_limiter
        self._ip_salt = ip_salt
        self._readiness = readiness
        self._log = get_logger("orchestrator.http")

    async def create_query(self, request: Request) -> Response:
        """`POST /api/v1/queries` — приём запроса пользователя."""
        try:
            self._guard.check(request.api_key)
            check_body_size(request.body_size)
            client_hash = hash_client_ip(request.client_ip, self._ip_salt)
            self._rate_limiter.check(client_hash, time.monotonic())
            body = request.body or {}
            command = SubmitQueryCommand(
                query_text=validate_query_text(body.get("query_text", "")),
                top_n=validate_top_n(body.get("top_n")),
                idempotency_key=validate_idempotency_key(request.idempotency_key),
                request_hash=canonical_request_hash(
                    {
                        "query_text": validate_query_text(body.get("query_text", "")),
                        "top_n": validate_top_n(body.get("top_n")),
                    }
                ),
                client_ip_hash=client_hash,
            )
            result = await self._submit_query.execute(command)
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(
            202,
            {
                "job_id": result.job_id,
                "query_id": result.query_id,
                "status": result.status,
                "created": result.created,
            },
        )

    async def list_jobs(self, request: Request) -> Response:
        """`GET /api/v1/jobs` — список заданий с keyset-пагинацией."""
        try:
            self._guard.check(request.api_key)
            query = request.query or {}
            page = await self._list_jobs.execute(
                limit=validate_page_size(query.get("limit")),
                cursor=decode_cursor(query.get("cursor")),
                status=validate_status_filter(query.get("status")),
            )
        except AppError as error:
            return self._error(error, request.correlation_id)
        body: dict[str, Any] = {"items": [presenters.job_to_json(item) for item in page.items]}
        if page.next_cursor:
            body["next_cursor"] = page.next_cursor
        return Response(200, body)

    async def get_job(self, job_id: str, request: Request) -> Response:
        """`GET /api/v1/jobs/{job_id}` — состояние задания."""
        try:
            self._guard.check(request.api_key)
            view = await self._get_job.execute(validate_uuid(job_id, "job_id"))
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(200, presenters.job_to_json(view))

    async def cancel_job(self, job_id: str, request: Request) -> Response:
        """`POST /api/v1/jobs/{job_id}/cancel` — запрос отмены."""
        try:
            self._guard.check(request.api_key)
            view = await self._cancel_job.execute(validate_uuid(job_id, "job_id"))
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(202, presenters.job_to_json(view))

    async def get_results(self, job_id: str, request: Request) -> Response:
        """`GET /api/v1/jobs/{job_id}/results` — снимок результата."""
        try:
            self._guard.check(request.api_key)
            view = await self._get_results.execute(validate_uuid(job_id, "job_id"))
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(200, presenters.results_to_json(view))

    async def get_result_item(self, item_id: str, request: Request) -> Response:
        """`GET /api/v1/results/items/{item_id}` — элемент выдачи целиком."""
        try:
            self._guard.check(request.api_key)
            item, query_text = await self._get_result_item.execute(validate_uuid(item_id, "item_id"))
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(200, presenters.item_to_json(item, query_text))

    async def get_model(self, request: Request) -> Response:
        """`GET /api/v1/model` — сведения об активной модели."""
        try:
            self._guard.check(request.api_key)
            view = await self._get_model_info.execute()
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(200, presenters.model_info_to_json(view))

    async def score(self, request: Request) -> Response:
        """`POST /api/v1/score` — прямой скоринг описания технологии."""
        try:
            self._guard.check(request.api_key)
            check_body_size(request.body_size)
            body = request.body or {}
            title, description = validate_score_request(
                body.get("title", ""), body.get("description", "")
            )
            view = await self._score_text.execute(
                title, description, bool(body.get("with_enrichment", False))
            )
        except AppError as error:
            return self._error(error, request.correlation_id)
        return Response(200, presenters.score_to_json(view))

    async def healthz(self) -> Response:
        """`GET /healthz` — процесс жив."""
        return Response(200, {"status": "ok"})

    async def readyz(self) -> Response:
        """`GET /readyz` — готовность зависимостей."""
        checks = await self._readiness() if self._readiness else {"database": True}
        ready = all(checks.values())
        return Response(
            200 if ready else 503,
            {"status": "ready" if ready else "not_ready", "checks": checks},
        )

    def _error(self, error: AppError, correlation_id: str) -> Response:
        """Отображает прикладную ошибку в статус и тело `Error` по OpenAPI."""
        status = 500
        for error_type, code in _STATUS_BY_ERROR:
            if isinstance(error, error_type):
                status = code
                break
        headers = {"Retry-After": "30"} if status == 429 else None
        self._log.warning(
            "api.request", error_code=error.error_code, status=status, message=error.message
        )
        return Response(
            status,
            presenters.error_to_json(
                error.error_code, error.message, correlation_id, retryable=status in (429, 502, 503)
            ),
            headers,
        )
