"""Трасса сбора (этап 2, аудит A3): сырые ответы источников и судьба каждого документа."""

from __future__ import annotations

import dataclasses
import gzip
import json
import unittest

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.application.ports import HttpStatusError, HttpTimeoutError
from collector.application.trace_recording import (
    DocumentEvent,
    begin_collection_trace,
    current_collection_trace,
    redact_url,
)
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import CollectionLimits, CollectionMode, SourceKey

from .test_run_collection import OWNER, RunCollectionTestBase


class _Http:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes

    async def get_json(self, url, *, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Limiter:
    async def acquire(self, source_key) -> None:  # noqa: ANN001
        return None

    async def penalize(self, source_key, retry_after) -> None:  # noqa: ANN001
        return None

    def defer(self, source_key, seconds) -> None:  # noqa: ANN001
        return None


class _Clock:
    def now(self):  # noqa: ANN201
        from datetime import UTC, datetime  # noqa: PLC0415

        return datetime(2026, 10, 3, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        return None


class _Adapter(BaseSourceAdapter):
    key = SourceKey.OPENALEX
    allowed_hosts = frozenset({"api.openalex.org"})


class _Traces:
    def __init__(self, fail: bool = False) -> None:
        self.responses: list = []
        self.events: list = []
        self.fail = fail

    async def append(self, collection_id, run_id, responses, events) -> None:  # noqa: ANN001
        if self.fail:
            raise RuntimeError("база недоступна")
        self.responses += responses
        self.events += events


class _ListAdapter:
    """Адаптер со списком документов: дубли, лимит на источник, отбраковка."""

    key = SourceKey.OPENALEX
    raw_meta_keys: frozenset[str] = frozenset()

    def __init__(self, documents: list[RawDocument]) -> None:
        self._documents = documents

    @property
    def http_requests(self) -> int:
        return 1

    async def search(self, terms, limits, deadline):  # noqa: ANN001, ANN201
        for document in self._documents:
            yield document


def raw(index: int, url: str | None = None, title: str | None = None) -> RawDocument:
    return RawDocument(url=url or f"https://example.org/paper-{index}", title=f"Статья {index}" if title is None else title,
                       text=f"Исследование ранней технологии номер {index} для навигации", matched_term="quantum sensors")


class RedactionTest(unittest.TestCase):
    def test_secret_parameters_are_removed_from_url(self) -> None:
        url = redact_url("https://user:pw@api.openalex.org/works?search=edge&api_key=SECRET", {"per-page": 50, "token": "T"})
        self.assertNotIn("SECRET", url)
        self.assertNotIn("pw", url)
        self.assertNotIn("=T", url)
        self.assertIn("search=edge", url)
        self.assertIn("per-page=50", url)


class FetchRecordingTest(unittest.IsolatedAsyncioTestCase):
    async def test_ok_429_and_timeout_are_recorded_with_body(self) -> None:
        adapter = _Adapter(_Http([{"results": [1]}, HttpStatusError(429, "лимит"), HttpTimeoutError("медленно"),
                                  HttpTimeoutError("медленно")]),
                           _Limiter(), _Clock())
        trace = begin_collection_trace()
        self.assertIs(current_collection_trace(), trace)
        self.assertEqual(await adapter.fetch_json("https://api.openalex.org/works", params={"api_key": "K", "q": "x"}),
                         {"results": [1]})
        with self.assertRaises(AdapterFailure):
            await adapter.fetch_json("https://api.openalex.org/works", params={"q": "y"})
        with self.assertRaises(AdapterFailure):
            await adapter.fetch_json("https://api.openalex.org/works", params={"q": "z"})
        responses, _ = trace.drain()
        self.assertEqual([r.outcome for r in responses], ["OK", "RATE_LIMITED", "TIMEOUT", "TIMEOUT"])  # каждая попытка
        self.assertEqual(json.loads(gzip.decompress(responses[0].body_gzip)), {"results": [1]})
        self.assertNotIn("K", responses[0].url.split("api_key=")[1][:3])
        self.assertIsNone(responses[1].body_gzip)
        self.assertEqual([r.seq for r in responses], [1, 2, 3, 4])


class RunCollectionTraceTest(RunCollectionTestBase):
    def _use_case(self, adapter, traces) -> RunCollection:  # noqa: ANN001
        return RunCollection(collections=self.collections, documents=self.documents, adapter_runs=self.runs,
                             adapters={SourceKey.OPENALEX: adapter}, classification=self.classification, clock=self.clock,
                             config=RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01),
                             traces=traces)

    async def test_every_document_gets_an_event(self) -> None:
        limits = dataclasses.replace(CollectionLimits.defaults(CollectionMode.SEARCH), max_documents_per_source=3)
        collection = await self.make_collection((SourceKey.OPENALEX,), limits)
        documents = [raw(1), raw(2), raw(1, url="https://example.org/paper-1"), raw(3, title=""), raw(4), raw(5)]
        traces = _Traces()
        await self._use_case(_ListAdapter(documents), traces).execute(collection, OWNER)
        events = [e.event for e in traces.events]
        self.assertEqual(events.count(DocumentEvent.ACCEPTED), 3)
        self.assertEqual(events.count(DocumentEvent.DEDUP), 1)
        self.assertEqual(events.count(DocumentEvent.REJECTED), 1)
        self.assertEqual(events.count(DocumentEvent.SOURCE_CAP), 1)
        self.assertEqual(len(traces.events), len(documents))  # ни один документ не пропал без события
        self.assertTrue(all(e.matched_term == "quantum sensors" for e in traces.events))
        self.assertEqual(sorted(e.seq for e in traces.events), list(range(1, len(documents) + 1)))

    async def test_trace_failure_does_not_fail_collection(self) -> None:
        collection = await self.make_collection((SourceKey.OPENALEX,))
        status = await self._use_case(_ListAdapter([raw(1), raw(2)]), _Traces(fail=True)).execute(collection, OWNER)
        self.assertIn(status.value, {"COMPLETED", "PARTIAL"})

    async def test_trace_disabled_records_nothing(self) -> None:
        collection = await self.make_collection((SourceKey.OPENALEX,))
        status = await self._use_case(_ListAdapter([raw(1), raw(2)]), None).execute(collection, OWNER)
        self.assertIn(status.value, {"COMPLETED", "PARTIAL"})


if __name__ == "__main__":
    unittest.main()
