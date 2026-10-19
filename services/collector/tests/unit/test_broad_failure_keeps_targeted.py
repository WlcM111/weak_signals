"""R-042 (COL-31, COL-33): временный отказ источника в широком проходе не исключает его из прицельного прохода
и второго круга (флаг WS_COLLECTOR_BROAD_FAILURE_KEEPS_TARGETED); блокирующие отказы исключают, как прежде."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.adapters.outbound.sources.hackernews import HackerNewsAdapter
from collector.application.dto import CollectionDraft
from collector.application.ports import HttpTimeoutError
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.domain.entities import AdapterRun
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import DEFAULT_POLICIES
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
    FakeRateLimiter,
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    ScriptedAdapter,
    raw_document,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
TERMS = SearchTerms(ru=("защита ИИ",), en=("ai security",), technologies=("x402",))
PAUSE = 3.5
SNOWBALL = "photonic tensor cores"
# Заголовки первого прицельного круга OpenAlex: из них второй круг извлекает SNOWBALL.
SEED_TITLES = ["Photonic tensor cores for inference", "Photonic tensor cores at wafer scale",
               *(f"Notes {word}" for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"))]
WINDOW_FAILURE = "Hacker News: не удалось 1 из 9 запросов; первый: "


def slug(term: str) -> str:
    return term.replace(" ", "-")


class FlakySource(ScriptedAdapter):
    """Дублёр источника: широкий проход отдаёт документы и кончается заданным отказом, прицельный отвечает."""

    supports_targeted = True

    def __init__(self, key: SourceKey, clock: FakeClock, failure: AdapterFailure | None = None,
                 seed: list[str] | None = None, broad: int = 2, advance: float = 0.0) -> None:
        documents = [raw_document(f"https://{key.value}.example/broad/{i}", title=f"broad-{key.value}-{i}",
                                  text=f"Широкий проход {key.value}, документ {i}") for i in range(broad)]
        super().__init__(key, documents, failure=failure, clock=clock, advance_seconds=advance)
        self.clock = clock
        self.seed = seed or []
        self.search_calls = 0
        self.targeted_calls: list[list[str]] = []
        self.slept_before_targeted: list[float] | None = None

    async def search(self, terms, limits, deadline):  # noqa: ANN001, ANN201
        self.search_calls += 1
        async for document in super().search(terms, limits, deadline):
            yield document

    async def search_targeted(self, technologies, per_term, since, deadline):  # noqa: ANN001, ANN201
        if self.slept_before_targeted is None:
            self.slept_before_targeted = list(self.clock.slept)
        self.targeted_calls.append(list(technologies))
        number = len(self.targeted_calls)
        for term in technologies:
            yield raw_document(f"https://{self.key.value}.example/r{number}/{slug(term)}", title=term,
                               text=f"{term}: {self.key.value}, круг {number}", matched_term=term)
        for index, title in enumerate(self.seed if number == 1 else []):
            yield raw_document(f"https://{self.key.value}.example/seed/{index}", title=title, matched_term="x402")


@dataclass
class Outcome:
    status: OperationStatus
    hn: FlakySource
    openalex: FlakySource
    stored: set[str]
    run: AdapterRun
    clock: FakeClock


def config(flag: bool, **extra: Any) -> RunCollectionConfig:
    """Конфигурация сбора; при выключенном флаге — без новых полей (как до правки)."""
    if flag:
        extra["broad_failure_keeps_targeted"] = True
    return RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01, **extra)


async def run_collection(adapters: dict[SourceKey, Any], clock: FakeClock, cfg: RunCollectionConfig,
                         unavailable: dict[SourceKey, AdapterErrorCode] | None = None,
                         ) -> tuple[OperationStatus, set[str], dict[SourceKey, AdapterRun]]:
    runs = InMemoryAdapterRunRepository()
    collections = InMemoryCollectionRepository(clock, runs)
    documents = InMemoryDocumentRepository(clock, collections)
    collection, _ = await collections.create_if_absent(CollectionDraft(
        idempotency_key="job-r042:collect", query_text="защита ИИ", mode=CollectionMode.SEARCH, terms=TERMS,
        limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=tuple(adapters)))
    collection.transition_to(OperationStatus.RUNNING, clock.now())
    classification = load_classification_config(CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml")
    status = await RunCollection(
        collections=collections, documents=documents, adapter_runs=runs, adapters=adapters,
        classification=classification, clock=clock, config=cfg, unavailable_sources=unavailable,
    ).execute(collection, "collector:test")
    stored = {document.url for document in documents.documents.values()}
    return status, stored, {run.source_key: run for run in await runs.list_runs(collection.collection_id)}


class BroadFailureTargetedTest(unittest.IsolatedAsyncioTestCase):
    """Дублёр HN отказывает в широком проходе; OpenAlex исправен и даёт второй круг."""

    async def collect(self, failure: AdapterFailure | None, *, flag: bool,
                      unavailable: dict[SourceKey, AdapterErrorCode] | None = None,
                      openalex_advance: float = 0.0) -> Outcome:
        clock = FakeClock()
        hn = FlakySource(SourceKey.HACKERNEWS, clock, failure=failure)
        openalex = FlakySource(SourceKey.OPENALEX, clock, seed=SEED_TITLES, broad=4, advance=openalex_advance)
        cfg = config(flag, **({"broad_retry_pause_seconds": PAUSE} if flag else {}))
        status, stored, runs = await run_collection({SourceKey.HACKERNEWS: hn, SourceKey.OPENALEX: openalex},
                                                    clock, cfg, unavailable)
        return Outcome(status, hn, openalex, stored, runs[SourceKey.HACKERNEWS], clock)

    async def test_flag_off_failed_source_skipped_in_targeted_and_snowball(self) -> None:
        out = await self.collect(AdapterFailure(AdapterErrorCode.TIMEOUT.value, WINDOW_FAILURE + "таймаут"),
                                 flag=False)
        self.assertEqual(out.hn.targeted_calls, [])  # прежнее поведение: ни прицельного, ни второго круга
        self.assertEqual(out.openalex.targeted_calls, [["x402"], [SNOWBALL]])
        self.assertFalse({url for url in out.stored if url.startswith("https://hackernews.example/r")})
        self.assertEqual((out.run.status, out.run.error_code), (OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value))
        self.assertIs(out.status, OperationStatus.PARTIAL)
        self.assertNotIn(PAUSE, out.clock.slept)

    async def test_transient_failure_keeps_source_in_targeted_and_snowball(self) -> None:
        for code in (AdapterErrorCode.TIMEOUT, AdapterErrorCode.HTTP_5XX, AdapterErrorCode.NETWORK_ERROR):
            with self.subTest(code=code.value):
                out = await self.collect(AdapterFailure(code.value, WINDOW_FAILURE + code.value), flag=True)
                self.assertEqual(out.hn.search_calls, 1)
                self.assertEqual(out.hn.targeted_calls, [["x402"], [SNOWBALL]])
                self.assertIn("https://hackernews.example/r1/x402", out.stored)
                self.assertIn(f"https://hackernews.example/r2/{slug(SNOWBALL)}", out.stored)
                self.assertIn(PAUSE, out.hn.slept_before_targeted or [])  # повтор — после паузы
                # отказ окна широкого прохода виден: запуск FAILED с его кодом, документы обоих проходов учтены
                self.assertEqual((out.run.status, out.run.error_code), (OperationStatus.FAILED, code.value))
                self.assertEqual(out.run.documents_new, 4)
                self.assertIs(out.status, OperationStatus.PARTIAL)

    async def test_pause_counted_from_failure(self) -> None:
        # широкий проход длился после отказа дольше паузы (OpenAlex: 4 × 5 с) — прицельный повтор без ожидания
        out = await self.collect(AdapterFailure(AdapterErrorCode.TIMEOUT.value, WINDOW_FAILURE + "таймаут"),
                                 flag=True, openalex_advance=5.0)
        self.assertEqual(out.hn.targeted_calls, [["x402"], [SNOWBALL]])
        self.assertEqual([seconds for seconds in out.clock.slept if seconds != 10_000], [])

    async def test_blocking_failure_still_excludes_source(self) -> None:
        # ошибка доступа и невременные отказы (429 после повторов, разбор ответа, бюджет запросов) — как прежде
        for code, message in ((AdapterErrorCode.HTTP_4XX, "источник ответил кодом 403"),
                              (AdapterErrorCode.RATE_LIMITED, "источник ограничил частоту запросов"),
                              (AdapterErrorCode.PARSE_ERROR, "ответ источника не разобран"),
                              (AdapterErrorCode.BUDGET_EXHAUSTED, "бюджет запросов источника на сбор исчерпан")):
            with self.subTest(code=code.value):
                out = await self.collect(AdapterFailure(code.value, message), flag=True)
                self.assertEqual(out.hn.targeted_calls, [])
                self.assertEqual(out.openalex.targeted_calls, [["x402"], [SNOWBALL]])
                self.assertEqual(out.run.error_code, code.value)

    async def test_disabled_or_keyless_source_not_requested(self) -> None:
        for code in (AdapterErrorCode.DISABLED, AdapterErrorCode.AUTH_MISSING):
            with self.subTest(code=code.value):
                out = await self.collect(None, flag=True, unavailable={SourceKey.HACKERNEWS: code})
                self.assertEqual((out.hn.search_calls, out.hn.targeted_calls), (0, []))
                self.assertEqual((out.run.status, out.run.error_code), (OperationStatus.FAILED, code.value))
                self.assertEqual(out.openalex.targeted_calls, [["x402"], [SNOWBALL]])


def story(url: str, title: str) -> dict[str, Any]:
    return {"objectID": url.rsplit("/", 1)[-1], "title": title, "url": url, "created_at_i": 1788256800}


class RoutedHnHttp:
    """HTTP-порт Hacker News: второе окно истории — таймаут (и при повторе), прочие окна и прицельные — ответы."""

    def __init__(self) -> None:
        self.windows: list[str] = []
        self.targeted: list[str] = []

    async def get_json(self, url: str, *, allowed_hosts: frozenset[str], params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> Any:
        params = dict(params or {})
        window = str(params["numericFilters"])
        if "created_at_i<" not in window:  # прицельный запрос: только нижняя граница даты
            query = str(params["query"])
            self.targeted.append(query)
            return {"hits": [story(f"https://news.example/{query}", f"{query} payments in production")]}
        if window not in self.windows:
            self.windows.append(window)
        number = self.windows.index(window)
        if number == 1:
            raise HttpTimeoutError("таймаут чтения")
        return {"hits": [story("https://news.example/broad", "Agent firewall ships")] if number == 0 else []}


class HackerNewsWindowTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """Настоящий адаптер HN: окно истории отказало после выдачи документов (hackernews.py:110-113)."""

    async def collect(self, flag: bool) -> tuple[RoutedHnHttp, set[str], AdapterRun]:
        clock = FakeClock()
        http = RoutedHnHttp()
        adapter = HackerNewsAdapter(http, FakeRateLimiter(), clock, history_months=24)
        adapter.policy = DEFAULT_POLICIES[SourceKey.HACKERNEWS]
        _, stored, runs = await run_collection({SourceKey.HACKERNEWS: adapter}, clock, config(flag))
        return http, stored, runs[SourceKey.HACKERNEWS]

    async def test_flag_off_no_targeted_requests_after_window_timeout(self) -> None:
        http, stored, run = await self.collect(flag=False)
        self.assertGreaterEqual(len(http.windows), 3)  # отказ окна не остановил остальные окна
        self.assertEqual(http.targeted, [])
        self.assertIn("https://news.example/broad", stored)
        self.assertEqual(run.error_code, AdapterErrorCode.TIMEOUT.value)

    async def test_flag_on_targeted_requests_follow_window_timeout(self) -> None:
        http, stored, run = await self.collect(flag=True)
        self.assertEqual(http.targeted, ["x402"])
        self.assertIn("https://news.example/x402", stored)  # документ прицельного прохода пришёл
        self.assertIn("https://news.example/broad", stored)
        self.assertEqual((run.status, run.error_code), (OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value))


if __name__ == "__main__":
    unittest.main()
