"""R-003: служебный путь GET /api/v1/jobs/{job_id}/trace за API-ключом (флаг WS_TRACE_API); без флага маршрута нет."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from unittest import mock

if importlib.util.find_spec("pydantic") is None:  # схемы HTTP требуют pydantic; в requirements-local.txt его нет
    raise unittest.SkipTest("нужен pydantic: тест HTTP-маршрута трассы выполняется в окружении сервиса")

from orchestrator.adapters.inbound.http import presenters, schemas  # noqa: F401,E402 - загружены до подмены fastapi
from orchestrator.adapters.inbound.http.deps import ApiKeyGuard, RateLimiter
from orchestrator.adapters.inbound.http.handlers import ApiHandlers, Request
from orchestrator.application.use_cases.cancel_job import CancelJob
from orchestrator.application.use_cases.get_job import GetJob, ListJobs
from orchestrator.application.use_cases.get_results import GetResultItem, GetResults
from orchestrator.application.use_cases.proxy import GetModelInfo, ScoreText
from orchestrator.application.use_cases.submit_query import SubmitQuery
from ws_common import ids, logging  # noqa: F401 - загружены до подмены fastapi

from ..fakes import FakeAnalyzer, FakeClock, InMemoryIdempotencyRepository, InMemoryResultRepository, \
    LinkedJobRepository, new_id

API_KEY = "test-api-key"
TRACE = ("GET", "/api/v1/jobs/{job_id}/trace")
# Маршруты API до R-003.
ROUTES = {
    ("POST", "/api/v1/queries"), ("GET", "/api/v1/jobs"), ("GET", "/api/v1/jobs/{job_id}"),
    ("POST", "/api/v1/jobs/{job_id}/cancel"), ("GET", "/api/v1/jobs/{job_id}/results"),
    ("GET", "/api/v1/results/items/{item_id}"), ("GET", "/api/v1/model"), ("POST", "/api/v1/score"),
    ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/metrics"),
}


class _App:
    """Замена FastAPI, которая только запоминает маршруты: fastapi в тестовой среде не установлен."""

    def __init__(self, **_: object) -> None:
        self.routes: dict[tuple[str, str], object] = {}

    def middleware(self, _kind: str):  # noqa: ANN201
        return lambda function: function

    def get(self, path: str, **_: object):  # noqa: ANN201
        return self._add("GET", path)

    def post(self, path: str, **_: object):  # noqa: ANN201
        return self._add("POST", path)

    def _add(self, method: str, path: str):  # noqa: ANN202
        def register(function):  # noqa: ANN001, ANN202
            self.routes[(method, path)] = function
            return function
        return register


class _JSONResponse:
    def __init__(self, status_code: int, content: dict, headers: dict | None = None) -> None:
        self.status_code, self.content, self.headers = status_code, content, headers


def load_app():  # noqa: ANN201
    """Модуль app.py с подменёнными fastapi и метриками (prometheus_client в тестовой среде тоже нет)."""
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI, fastapi.Request, fastapi.Response = _App, object, object
    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = _JSONResponse
    metrics = types.ModuleType("ws_common.metrics")
    metrics.HTTP_REQUESTS, metrics.render_metrics = mock.Mock(), lambda: b""
    with mock.patch.dict(sys.modules, {"fastapi": fastapi, "fastapi.responses": responses, "ws_common.metrics": metrics}):
        sys.modules.pop("orchestrator.adapters.inbound.http.app", None)
        return importlib.import_module("orchestrator.adapters.inbound.http.app")


class TraceApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.idempotency = InMemoryIdempotencyRepository()
        self.jobs = LinkedJobRepository(self.idempotency, self.clock)
        self.results = InMemoryResultRepository(self.jobs)

    def handlers(self, trace: bool) -> ApiHandlers:
        extra = {}
        if trace:
            from orchestrator.application.use_cases.get_results import GetTrace  # noqa: PLC0415

            extra["get_trace"] = GetTrace(self.jobs, self.results)
        analyzer = FakeAnalyzer()
        return ApiHandlers(
            submit_query=SubmitQuery(self.jobs, self.idempotency, self.clock, max_pending=3),
            get_job=GetJob(self.jobs, self.results), list_jobs=ListJobs(self.jobs, self.results),
            cancel_job=CancelJob(self.jobs, self.results), get_results=GetResults(self.jobs, self.results),
            get_result_item=GetResultItem(self.results, self.jobs), get_model_info=GetModelInfo(analyzer),
            score_text=ScoreText(analyzer), guard=ApiKeyGuard(API_KEY), rate_limiter=RateLimiter(limit_per_minute=1000),
            ip_salt="salt", **extra)

    @staticmethod
    def request(**kwargs) -> Request:  # noqa: ANN003
        kwargs.setdefault("api_key", API_KEY)
        return Request(correlation_id="corr-1", client_ip="10.0.0.1", **kwargs)

    async def job_with_two_runs(self) -> str:
        created = await self.handlers(trace=False).create_query(
            self.request(idempotency_key="key-0001:trace", body={"query_text": "фотонные сенсоры", "top_n": 5}))
        job_id = created.body["job_id"]
        await self.results.save_trace(job_id, "narrate", {"run": 1}, keep_previous=True)
        await self.results.save_trace(job_id, "narrate", {"run": 2}, keep_previous=True)
        return job_id

    async def test_trace_with_flag_returns_last_run_and_previous_runs(self) -> None:
        job_id = await self.job_with_two_runs()
        handlers = self.handlers(trace=True)
        response = await handlers.get_trace(job_id, self.request())
        self.assertEqual(response.status, 200)
        self.assertEqual((response.body["job_id"], response.body["stage"]), (job_id, "narrate"))
        self.assertEqual(response.body["payload"], {"run": 2})
        self.assertEqual(response.body["stages"], ["narrate", "narrate@1"])
        previous = await handlers.get_trace(job_id, self.request(query={"stage": "narrate@1"}))
        self.assertEqual((previous.status, previous.body["payload"]), (200, {"run": 1}))

    async def test_trace_requires_api_key_and_valid_ids(self) -> None:
        job_id = await self.job_with_two_runs()
        handlers = self.handlers(trace=True)
        self.assertEqual((await handlers.get_trace(job_id, self.request(api_key=None))).status, 401)
        self.assertEqual((await handlers.get_trace(new_id(999), self.request())).status, 404)
        self.assertEqual((await handlers.get_trace(job_id, self.request(query={"stage": "collect"}))).status, 404)
        self.assertEqual((await handlers.get_trace(job_id, self.request(query={"stage": "narrate;--"}))).status, 400)
        self.assertEqual((await handlers.get_trace("не-uuid", self.request())).status, 400)

    async def test_handler_without_flag_is_not_found(self) -> None:
        job_id = await self.job_with_two_runs()
        response = await self.handlers(trace=False).get_trace(job_id, self.request())
        self.assertEqual((response.status, response.body["code"]), (404, "NOT_FOUND"))

    async def test_route_registered_only_with_flag(self) -> None:
        job_id = await self.job_with_two_runs()
        app = load_app().create_app(self.handlers(trace=True), trace_api=True)
        self.assertEqual(set(app.routes), ROUTES | {TRACE})
        http = types.SimpleNamespace(headers={"x-api-key": API_KEY}, client=None, query_params={"stage": "narrate"})
        response = await app.routes[TRACE](job_id, http)
        self.assertEqual((response.status_code, response.content["payload"]), (200, {"run": 2}))

    def test_routes_unchanged_without_flag(self) -> None:
        app = load_app().create_app(self.handlers(trace=False))
        self.assertEqual(set(app.routes), ROUTES)


if __name__ == "__main__":
    unittest.main()
