"""R-040: отказ по одной технологии не обрывает прицельный поиск источника (WS_COLLECTOR_TARGETED_PER_TECH_ERRORS)."""

from __future__ import annotations

import unittest
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.adapters.outbound.sources.crossref import CrossrefAdapter
from collector.adapters.outbound.sources.github import MAX_TARGETED_TERMS, GithubAdapter
from collector.adapters.outbound.sources.hackernews import HackerNewsAdapter
from collector.adapters.outbound.sources.huggingface import HuggingFaceAdapter
from collector.adapters.outbound.sources.openalex import OpenAlexAdapter
from collector.adapters.outbound.sources.semantic_scholar import SemanticScholarAdapter
from collector.application.dto import CollectionDraft
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.domain.errors import AdapterFailure
from collector.domain.values import (
    AdapterErrorCode,
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
)

from ..fakes import (
    FakeClock,
    FakeHttpClient,
    FakeRateLimiter,
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    ScriptedAdapter,
    raw_document,
    status_error,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
TECHNOLOGIES = ("kv cache offloading", "optical circuit switching", "x402 payments", "mcp security scanners",
                "photonic interconnects")


def slug(term: str) -> str:
    return term.replace(" ", "-")


class FailingTermAdapter(ScriptedAdapter):
    """Дублёр прицельного адаптера: технологии обходятся в цикле, как в настоящих адаптерах; на `failures` — отказ."""

    supports_targeted = True

    def __init__(self, failures: dict[str, Exception]) -> None:
        super().__init__(SourceKey.OPENALEX, [raw_document("https://openalex.example.org/broad", "Обзор направления")])
        self.failures = failures
        self.calls: list[list[str]] = []
        self.requested: list[str] = []

    async def search_targeted(self, technologies, per_term, since, deadline):  # noqa: ANN001, ANN201
        self.calls.append(list(technologies))
        for term in technologies:
            self.requested.append(term)
            if term in self.failures:
                raise self.failures[term]
            yield raw_document(f"https://openalex.example.org/{slug(term)}", f"{term} in production", matched_term=term)


class SnowballAdapter(FailingTermAdapter):
    """Второй круг: заголовки прицельного прохода дают новые названия; отказ — на первом из них."""

    TITLES = {
        "kv cache offloading": ("Tiered memory pooling for inference clusters", "KV cache offloading engine"),
        "optical circuit switching": ("Tiered memory pooling at rack scale", "Optical circuit switching fabrics"),
        "x402 payments": ("Silicon photonics transceivers for racks", "Agentic payment rails"),
        "mcp security scanners": ("Silicon photonics transceivers in datacenters", "MCP server audit tooling"),
        "photonic interconnects": ("Photonic interconnects roadmap", "Chiplet interconnect standards"),
    }

    def __init__(self) -> None:
        super().__init__({})
        self.snowball: list[str] = []

    async def search_targeted(self, technologies, per_term, since, deadline):  # noqa: ANN001, ANN201
        for term in technologies:
            if term in self.TITLES:
                for i, title in enumerate(self.TITLES[term]):
                    yield raw_document(f"https://openalex.example.org/{slug(term)}/{i}", title, matched_term=term)
                continue
            self.snowball.append(term)
            if len(self.snowball) == 1:
                raise AdapterFailure(AdapterErrorCode.TIMEOUT.value, "таймаут запроса к источнику", retryable=True)
            yield raw_document(f"https://openalex.example.org/snowball/{slug(term)}", f"{term} benchmark",
                               matched_term=term)


class TermHttpClient:
    """HTTP-дублёр: ответ по технологии в параметрах запроса; на технологию `failing` — код `status` каждый раз."""

    def __init__(self, empty: object, found, failing: str, status: int) -> None:  # noqa: ANN001
        self.empty, self.found, self.failing, self.status = empty, found, failing, status
        self.terms: list[str] = []  # технология каждого прицельного запроса (с повторами)

    async def get_json(self, url, *, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
        text = " ".join(str(value) for value in (params or {}).values())
        term = next((item for item in TECHNOLOGIES if item in text), None)
        if term is None:
            return self.empty  # широкий проход
        self.terms.append(term)
        if term == self.failing:
            raise status_error(self.status)
        return self.found(term)


# Шесть адаптеров из COL-03: фабрика, пустой ответ, ответ с одним документом по технологии.
REAL_ADAPTERS = {
    SourceKey.OPENALEX: (
        lambda http, clock: OpenAlexAdapter(http, FakeRateLimiter(), clock, api_key="k"), {"results": []},
        lambda term: {"results": [{"id": f"https://openalex.org/W-{slug(term)}", "title": f"{term} study",
                                   "publication_date": "2025-03-01"}]}),
    SourceKey.SEMANTIC_SCHOLAR: (
        lambda http, clock: SemanticScholarAdapter(http, FakeRateLimiter(), clock, api_key="k"), {"data": []},
        lambda term: {"data": [{"paperId": slug(term), "title": f"{term} study", "publicationDate": "2025-03-01"}]}),
    SourceKey.CROSSREF: (
        lambda http, clock: CrossrefAdapter(http, FakeRateLimiter(), clock, "team@example.org"),
        {"message": {"items": []}},
        lambda term: {"message": {"items": [{"DOI": f"10.5555/{slug(term)}", "title": [f"{term} study"],
                                             "issued": {"date-parts": [[2025, 3, 1]]}}]}}),
    SourceKey.GITHUB: (
        lambda http, clock: GithubAdapter(http, FakeRateLimiter(), clock), {"items": []},
        lambda term: {"items": [{"html_url": f"https://github.com/lab/{slug(term)}", "full_name": f"lab/{slug(term)}",
                                 "description": f"{term} toolkit", "created_at": "2025-03-01T00:00:00Z"}]}),
    SourceKey.HACKERNEWS: (
        lambda http, clock: HackerNewsAdapter(http, FakeRateLimiter(), clock), {"hits": []},
        lambda term: {"hits": [{"title": f"{term} study", "url": f"https://blog.example.com/{slug(term)}",
                                "objectID": slug(term), "created_at": "2025-03-01T00:00:00Z"}]}),
    SourceKey.HUGGINGFACE: (
        lambda http, clock: HuggingFaceAdapter(http, FakeRateLimiter(), clock), [],
        lambda term: [{"id": f"lab/{slug(term)}", "createdAt": "2025-03-01T00:00:00Z", "likes": 1}]),
}


class TargetedRunBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.classification = load_classification_config(CONFIG_DIR / "trust_rules.yaml",
                                                         CONFIG_DIR / "rss_domains.yaml")

    async def run_collection(self, adapter, clock: FakeClock, config: RunCollectionConfig,  # noqa: ANN001, ANN201
                             technologies: tuple[str, ...] = TECHNOLOGIES):
        """Сбор SEARCH по одному источнику: широкий проход, затем прицельный по технологиям темы.
        Возвращает статус сбора, запуск источника, технологии сохранённых документов и названия второго круга."""
        runs = InMemoryAdapterRunRepository()
        collections = InMemoryCollectionRepository(clock, runs)
        documents = InMemoryDocumentRepository(clock, collections)
        collection, _ = await collections.create_if_absent(CollectionDraft(
            idempotency_key="job-r040:collect", query_text="защита ИИ", mode=CollectionMode.SEARCH,
            terms=SearchTerms(ru=("защита ИИ",), en=("ai security",), technologies=technologies),
            limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=(adapter.key,)))
        collection.transition_to(OperationStatus.RUNNING, clock.now())
        status = await RunCollection(
            collections=collections, documents=documents, adapter_runs=runs, adapters={adapter.key: adapter},
            classification=self.classification, clock=clock, config=config,
        ).execute(collection, "collector:test")
        run = (await runs.list_runs(collection.collection_id))[0]
        found = {item.draft.matched_term for item in documents.persisted}
        return status, run, found, collections.mined.get(collection.collection_id, ())


def config(rounds: int = 1, **flag: bool) -> RunCollectionConfig:
    return RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01,
                               targeted_rounds=rounds, **flag)


class ScriptedTermFailureTest(TargetedRunBase):
    """Механизм в RunCollection на дублёре адаптера."""

    async def test_failure_on_first_technology_does_not_stop_source_with_flag(self) -> None:
        adapter = FailingTermAdapter({TECHNOLOGIES[0]: AdapterFailure(AdapterErrorCode.HTTP_5XX.value,
                                                                      "источник ответил кодом 503", retryable=True)})
        status, run, found, _ = await self.run_collection(adapter, FakeClock(), config(targeted_per_tech_errors=True))
        self.assertEqual(adapter.requested, list(TECHNOLOGIES))
        self.assertEqual(found & set(TECHNOLOGIES), set(TECHNOLOGIES[1:]))
        # отказ не пропал: запуск источника помечен частичным отказом, сбор — PARTIAL, как и прежде
        self.assertIs(run.status, OperationStatus.FAILED)
        self.assertEqual(run.error_code, AdapterErrorCode.HTTP_5XX.value)
        self.assertIn("1 из 5", run.error_message)
        self.assertIn(TECHNOLOGIES[0], run.error_message)
        self.assertIs(status, OperationStatus.PARTIAL)

    async def test_flag_off_keeps_previous_behavior(self) -> None:
        adapter = FailingTermAdapter({TECHNOLOGIES[0]: AdapterFailure(AdapterErrorCode.HTTP_5XX.value,
                                                                      "источник ответил кодом 503", retryable=True)})
        status, run, found, _ = await self.run_collection(adapter, FakeClock(), config())
        self.assertEqual(adapter.calls, [list(TECHNOLOGIES)])  # один вызов со всем списком, как прежде
        self.assertEqual(adapter.requested, [TECHNOLOGIES[0]])
        self.assertEqual(found & set(TECHNOLOGIES), set())
        self.assertEqual((run.status, run.error_code, run.error_message),
                         (OperationStatus.FAILED, AdapterErrorCode.HTTP_5XX.value, "источник ответил кодом 503"))
        self.assertIs(status, OperationStatus.PARTIAL)

    async def test_blocking_failures_still_stop_source(self) -> None:
        blocking = [
            AdapterFailure(AdapterErrorCode.RATE_LIMITED.value, "суточный лимит источника исчерпан; сброс через 60 мин",
                           retryable=False),
            AdapterFailure(AdapterErrorCode.RATE_LIMITED.value, "источник ответил 429", retryable=True),
            AdapterFailure(AdapterErrorCode.BUDGET_EXHAUSTED.value, "бюджет запросов источника на сбор исчерпан: 60"),
            AdapterFailure(AdapterErrorCode.AUTH_MISSING.value, "не задан ключ доступа к источнику"),
            AdapterFailure(AdapterErrorCode.DISABLED.value, "адаптер отключён"),
        ]
        for failure in blocking:
            with self.subTest(code=failure.code, message=failure.message):
                adapter = FailingTermAdapter({TECHNOLOGIES[0]: failure})
                _status, run, found, _ = await self.run_collection(adapter, FakeClock(),
                                                                   config(targeted_per_tech_errors=True))
                self.assertEqual(adapter.requested, [TECHNOLOGIES[0]])
                self.assertEqual(found & set(TECHNOLOGIES), set())
                self.assertEqual((run.status, run.error_code, run.error_message),
                                 (OperationStatus.FAILED, failure.code, failure.message))

    async def test_second_round_continues_after_failed_technology_only_with_flag(self) -> None:
        for flag in (True, False):
            with self.subTest(flag=flag):
                adapter = SnowballAdapter()
                _status, run, found, mined = await self.run_collection(
                    adapter, FakeClock(), config(rounds=2, targeted_per_tech_errors=flag))
                self.assertGreaterEqual(len(mined), 2)  # названия второго круга из заголовков прицельного прохода
                expected = list(mined) if flag else [mined[0]]
                self.assertEqual(adapter.snowball, expected)
                self.assertEqual(found & set(mined), set(expected[1:]))
                self.assertEqual((run.status, run.error_code), (OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value))


class RealAdaptersTermFailureTest(TargetedRunBase):
    """Шесть настоящих адаптеров (HTTP-дублёр): 503 на первой технологии после повтора — HTTP_5XX (k03 A)."""

    async def collect(self, key: SourceKey, status_code: int, **flag: bool):  # noqa: ANN201
        factory, empty, response = REAL_ADAPTERS[key]
        http = TermHttpClient(empty, response, failing=TECHNOLOGIES[0], status=status_code)
        clock = FakeClock()
        _status, run, terms_found, _ = await self.run_collection(factory(http, clock), clock, config(**flag))
        return http.terms, run, terms_found & set(TECHNOLOGIES)

    async def test_other_technologies_requested_and_found_with_flag(self) -> None:
        for key in REAL_ADAPTERS:
            with self.subTest(source=key.value):
                terms, run, matched = await self.collect(key, 503, targeted_per_tech_errors=True)
                self.assertEqual(terms, [TECHNOLOGIES[0], *TECHNOLOGIES])  # 503 → один повтор, затем остальные
                self.assertEqual(matched, set(TECHNOLOGIES[1:]))
                self.assertEqual((run.status, run.error_code),
                                 (OperationStatus.FAILED, AdapterErrorCode.HTTP_5XX.value))

    async def test_flag_off_other_technologies_not_requested(self) -> None:
        for key in REAL_ADAPTERS:
            with self.subTest(source=key.value):
                terms, run, matched = await self.collect(key, 503)
                self.assertEqual(terms, [TECHNOLOGIES[0], TECHNOLOGIES[0]])
                self.assertEqual(matched, set())
                self.assertEqual((run.status, run.error_code),
                                 (OperationStatus.FAILED, AdapterErrorCode.HTTP_5XX.value))

    async def test_http_status_decides_whether_source_stops(self) -> None:
        # 404 — отказ одной технологии; 401/403 — отказ доступа, 429 — ограничение частоты с паузой: источник стоит
        stop = [TECHNOLOGIES[0]]
        expected = {404: (list(TECHNOLOGIES), AdapterErrorCode.HTTP_4XX), 401: (stop, AdapterErrorCode.HTTP_4XX),
                    403: (stop, AdapterErrorCode.HTTP_4XX), 429: (stop, AdapterErrorCode.RATE_LIMITED)}
        for status_code, (requested, code) in expected.items():
            with self.subTest(status=status_code):
                terms, run, matched = await self.collect(SourceKey.OPENALEX, status_code, targeted_per_tech_errors=True)
                self.assertEqual(terms, requested)
                self.assertEqual(matched, set(requested[1:]))
                self.assertEqual((run.status, run.error_code), (OperationStatus.FAILED, code.value))

    async def test_github_keeps_its_technology_limit(self) -> None:
        technologies = tuple(f"tech {i}" for i in range(MAX_TARGETED_TERMS + 5))
        for flag in (True, False):
            with self.subTest(flag=flag):
                http, clock = FakeHttpClient([{"items": []}] * (len(technologies) + 1)), FakeClock()
                await self.run_collection(GithubAdapter(http, FakeRateLimiter(), clock), clock,
                                          config(targeted_per_tech_errors=flag), technologies)
                targeted = [call for _url, call in http.calls if "pushed:>=" in call["params"]["q"]]
                self.assertEqual(len(targeted), MAX_TARGETED_TERMS)


if __name__ == "__main__":
    unittest.main()
