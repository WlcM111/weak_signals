"""Контрактные тесты HTTP: пути, коды ответов и тела по `orchestrator.openapi.yaml`.

Проверяются обработчики, а не веб-сервер: маршруты FastAPI — тонкие обёртки над ними, поэтому
поведение API (валидация, идемпотентность, пагинация, коды ошибок) покрыто без поднятия uvicorn.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestrator.adapters.inbound.http.deps import ApiKeyGuard, RateLimiter
from orchestrator.adapters.inbound.http.handlers import ApiHandlers, Request
from orchestrator.application.use_cases.cancel_job import CancelJob
from orchestrator.application.use_cases.get_job import GetJob, ListJobs
from orchestrator.application.use_cases.get_results import GetResultItem, GetResults
from orchestrator.application.use_cases.proxy import GetModelInfo, ScoreText
from orchestrator.application.use_cases.submit_query import SubmitQuery
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.values import JobStatus

from ..fakes import (
    FakeAnalyzer,
    FakeClock,
    InMemoryIdempotencyRepository,
    InMemoryResultRepository,
    LinkedJobRepository,
    new_id,
)

OPENAPI = Path(__file__).resolve().parents[4] / "docs" / "api" / "orchestrator.openapi.yaml"
API_KEY = "test-api-key"


class HandlersHarness(unittest.IsolatedAsyncioTestCase):
    """Сборка обработчиков на дублёрах портов."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.idempotency = InMemoryIdempotencyRepository()
        self.jobs = LinkedJobRepository(self.idempotency, self.clock)
        self.results = InMemoryResultRepository(self.jobs)
        self.analyzer = FakeAnalyzer()
        self.rate_limiter = RateLimiter(limit_per_minute=1000)
        self.handlers = ApiHandlers(
            submit_query=SubmitQuery(self.jobs, self.idempotency, self.clock, max_pending=3),
            get_job=GetJob(self.jobs, self.results),
            list_jobs=ListJobs(self.jobs, self.results),
            cancel_job=CancelJob(self.jobs, self.results),
            get_results=GetResults(self.jobs, self.results),
            get_result_item=GetResultItem(self.results, self.jobs),
            get_model_info=GetModelInfo(self.analyzer),
            score_text=ScoreText(self.analyzer),
            guard=ApiKeyGuard(API_KEY),
            rate_limiter=self.rate_limiter,
            ip_salt="salt",
        )

    def request(self, **kwargs) -> Request:  # noqa: ANN003
        """Запрос с корректным ключом API по умолчанию."""
        kwargs.setdefault("api_key", API_KEY)
        kwargs.setdefault("correlation_id", "corr-1")
        kwargs.setdefault("client_ip", "10.0.0.1")
        return Request(**kwargs)

    async def submit(  # noqa: ANN201
        self, key: str = "key-0001:submit", text: str = "слабые сигналы в ИИ", top_n: int = 15
    ):
        """Создаёт задание через API."""
        return await self.handlers.create_query(
            self.request(idempotency_key=key, body={"query_text": text, "top_n": top_n})
        )


class CreateQueryTest(HandlersHarness):
    """`POST /api/v1/queries`."""

    async def test_accepts_query(self) -> None:
        response = await self.submit()
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["status"], JobStatus.QUEUED.value)
        self.assertTrue(response.body["created"])
        self.assertIn("job_id", response.body)

    async def test_same_key_and_body_returns_same_job(self) -> None:
        first = await self.submit()
        second = await self.submit()
        self.assertEqual(second.status, 202)
        self.assertFalse(second.body["created"])
        self.assertEqual(first.body["job_id"], second.body["job_id"])

    async def test_same_key_other_body_conflicts(self) -> None:
        await self.submit()
        response = await self.submit(text="другой запрос")
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["code"], "IDEMPOTENCY_CONFLICT")

    async def test_missing_idempotency_key(self) -> None:
        response = await self.handlers.create_query(
            self.request(body={"query_text": "запрос", "top_n": 15})
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body["code"], "VALIDATION_ERROR")

    async def test_invalid_query_text(self) -> None:
        response = await self.submit(text="и")
        self.assertEqual(response.status, 400)

    async def test_invalid_top_n(self) -> None:
        response = await self.submit(top_n=100)
        self.assertEqual(response.status, 400)

    async def test_unauthorized_without_key(self) -> None:
        response = await self.handlers.create_query(
            Request(idempotency_key="key-0002:submit", body={"query_text": "запрос"})
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body["code"], "UNAUTHORIZED")

    async def test_queue_full(self) -> None:
        for index in range(3):
            await self.submit(key=f"key-{index:04d}:submit", text=f"запрос {index}")
        response = await self.submit(key="key-0009:submit", text="ещё запрос")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.body["code"], "QUEUE_FULL")
        self.assertEqual(response.headers["Retry-After"], "30")

    async def test_rate_limited(self) -> None:
        self.rate_limiter.limit_per_minute = 1
        await self.submit(key="key-0010:submit")
        response = await self.submit(key="key-0011:submit", text="второй запрос")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.body["code"], "RATE_LIMITED")

    async def test_body_size_limit(self) -> None:
        response = await self.handlers.create_query(
            self.request(
                idempotency_key="key-0012:submit",
                body={"query_text": "запрос"},
                body_size=128 * 1024,
            )
        )
        self.assertEqual(response.status, 400)


class JobsTest(HandlersHarness):
    """`GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, отмена."""

    async def test_get_job(self) -> None:
        created = await self.submit()
        response = await self.handlers.get_job(created.body["job_id"], self.request())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["query_text"], "слабые сигналы в ИИ")
        self.assertEqual(response.body["progress"]["stage"], JobStatus.QUEUED.value)

    async def test_get_unknown_job(self) -> None:
        response = await self.handlers.get_job(new_id(999), self.request())
        self.assertEqual(response.status, 404)

    async def test_invalid_job_id(self) -> None:
        response = await self.handlers.get_job("не-uuid", self.request())
        self.assertEqual(response.status, 400)

    async def test_list_jobs_pagination(self) -> None:
        for index in range(3):
            await self.submit(key=f"key-{index:04d}:submit", text=f"запрос {index}")
            self.clock.advance(1)
        first = await self.handlers.list_jobs(self.request(query={"limit": 2}))
        self.assertEqual(len(first.body["items"]), 2)
        self.assertIn("next_cursor", first.body)
        second = await self.handlers.list_jobs(
            self.request(query={"limit": 2, "cursor": first.body["next_cursor"]})
        )
        self.assertEqual(len(second.body["items"]), 1)
        seen = [item["job_id"] for item in first.body["items"] + second.body["items"]]
        self.assertEqual(len(set(seen)), 3)

    async def test_list_filter_by_status(self) -> None:
        await self.submit()
        response = await self.handlers.list_jobs(self.request(query={"status": "COMPLETED"}))
        self.assertEqual(response.body["items"], [])

    async def test_list_invalid_status(self) -> None:
        response = await self.handlers.list_jobs(self.request(query={"status": "НЕТ"}))
        self.assertEqual(response.status, 400)

    async def test_cancel_queued_job(self) -> None:
        created = await self.submit()
        response = await self.handlers.cancel_job(created.body["job_id"], self.request())
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["status"], JobStatus.CANCELLED.value)

    async def test_cancel_terminal_job_conflicts(self) -> None:
        created = await self.submit()
        await self.handlers.cancel_job(created.body["job_id"], self.request())
        response = await self.handlers.cancel_job(created.body["job_id"], self.request())
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["code"], "JOB_NOT_CANCELLABLE")


class ResultsTest(HandlersHarness):
    """`GET /api/v1/jobs/{id}/results` и элемент выдачи."""

    async def test_results_not_ready_before_narrating(self) -> None:
        created = await self.submit()
        response = await self.handlers.get_results(created.body["job_id"], self.request())
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["code"], "RESULTS_NOT_READY")

    async def test_results_shape(self) -> None:
        from ..fakes import make_candidate, make_document
        from orchestrator.application.stages.narrate import NarrateConfig, run_narrate
        from ..fakes import FakeAnalyzer as Analyzer, FakeCollector, FakeInsight

        created = await self.submit()
        job_id = created.body["job_id"]
        job = self.jobs.jobs[job_id]
        job.transition(JobStatus.COLLECTING, self.clock.now())
        job.transition(JobStatus.ANALYZING, self.clock.now())
        job.transition(JobStatus.NARRATING, self.clock.now())
        documents = {make_document(index).document_id: make_document(index) for index in (1, 2)}

        async def no_check() -> None:
            """Отмена не запрашивается."""

        await run_narrate(
            analyzer=Analyzer(candidates=[make_candidate(1, rank=1)]),
            collector=FakeCollector(documents=documents),
            insight=FakeInsight(),
            results=self.results,
            job_id=job_id,
            query_text=job.query_text,
            analysis_id=new_id(20),
            config=NarrateConfig(top_n=15),
            check=no_check,
        )
        response = await self.handlers.get_results(job_id, self.request())
        self.assertEqual(response.status, 200)
        body = response.body
        self.assertEqual(set(body), {"job_id", "query_text", "status", "items", "excluded", "stats"})
        item = body["items"][0]
        self.assertEqual(
            set(item),
            {
                "item_id", "rank", "title_ru", "score", "confidence_band", "key_predictors",
                "decision_explanation_ru", "narrative_status", "source_count",
            },
        )
        detail = await self.handlers.get_result_item(item["item_id"], self.request())
        self.assertEqual(detail.status, 200)
        for field in (
            "description_ru", "advantage_ru", "case_example_ru", "explanation_ru",
            "features", "sources", "provenance", "title_auto", "query_text",
        ):
            self.assertIn(field, detail.body)
        source = detail.body["sources"][0]
        for field in (
            "title", "url", "published_at", "source_type", "language_code",
            "trust_level", "summary_ru", "summary_kind",
        ):
            self.assertIn(field, source)

    async def test_unknown_item(self) -> None:
        response = await self.handlers.get_result_item(new_id(555), self.request())
        self.assertEqual(response.status, 404)


class ProxyTest(HandlersHarness):
    """`GET /api/v1/model` и `POST /api/v1/score`."""

    async def test_model_info(self) -> None:
        response = await self.handlers.get_model(self.request())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["feature_schema_version"], "v1")
        self.assertIn("metrics", response.body)

    async def test_score(self) -> None:
        response = await self.handlers.score(
            self.request(body={"title": "Нейроморфные чипы", "description": "прототип"})
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["decision"], "WEAK_SIGNAL")
        self.assertTrue(response.body["features"])

    async def test_score_validation(self) -> None:
        response = await self.handlers.score(self.request(body={"title": "т"}))
        self.assertEqual(response.status, 400)


class HealthTest(HandlersHarness):
    """`/healthz` и `/readyz`."""

    async def test_healthz(self) -> None:
        response = await self.handlers.healthz()
        self.assertEqual((response.status, response.body["status"]), (200, "ok"))

    async def test_readyz_default(self) -> None:
        response = await self.handlers.readyz()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ready")


class OpenApiConformanceTest(unittest.TestCase):
    """Все пути нормативного OpenAPI имеют обработчик, и наоборот."""

    HANDLER_BY_PATH = {
        "/api/v1/queries": "create_query",
        "/api/v1/jobs": "list_jobs",
        "/api/v1/jobs/{job_id}": "get_job",
        "/api/v1/jobs/{job_id}/cancel": "cancel_job",
        "/api/v1/jobs/{job_id}/results": "get_results",
        "/api/v1/results/items/{item_id}": "get_result_item",
        "/api/v1/model": "get_model",
        "/api/v1/score": "score",
        "/healthz": "healthz",
        "/readyz": "readyz",
        "/metrics": None,
    }

    def test_every_path_has_handler(self) -> None:
        spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        for path in spec["paths"]:
            with self.subTest(path=path):
                self.assertIn(path, self.HANDLER_BY_PATH, f"путь {path} не реализован")
                handler = self.HANDLER_BY_PATH[path]
                if handler:
                    self.assertTrue(hasattr(ApiHandlers, handler))

    def test_no_extra_handlers(self) -> None:
        spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
        declared = set(spec["paths"])
        self.assertEqual(set(self.HANDLER_BY_PATH) - declared, set())


if __name__ == "__main__":
    unittest.main()
