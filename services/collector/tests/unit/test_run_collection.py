"""Сценарий RunCollection: интерливинг, дедупликация, лимиты, отмена, аренда, итоговый статус."""

from __future__ import annotations

import unittest
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.domain.entities import Collection
from collector.domain.errors import AdapterFailure, LeaseLost
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
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    ScriptedAdapter,
    raw_document,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
OWNER = "collector:test"


class RunCollectionTestBase(unittest.IsolatedAsyncioTestCase):
    """Общая сборка сценария с тестовыми заменами портов."""

    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.runs = InMemoryAdapterRunRepository()
        self.collections = InMemoryCollectionRepository(self.clock, self.runs)
        self.documents = InMemoryDocumentRepository(self.clock, self.collections)
        self.classification = load_classification_config(
            CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml"
        )

    async def make_collection(
        self, sources: tuple[SourceKey, ...], limits: CollectionLimits | None = None
    ) -> Collection:
        """Коллекция в статусе RUNNING (как после захвата воркером)."""
        from collector.application.dto import CollectionDraft

        collection, _ = await self.collections.create_if_absent(
            CollectionDraft(
                idempotency_key="job-0001:collect",
                query_text="технологии защиты ИИ-систем",
                mode=CollectionMode.SEARCH,
                terms=SearchTerms(ru=("защита ИИ",), en=("ai security",)),
                limits=limits or CollectionLimits.defaults(CollectionMode.SEARCH),
                sources=sources,
            )
        )
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        return collection

    def build(self, adapters: dict, **kwargs: object) -> RunCollection:  # noqa: ANN001 - помощник тестов
        """Собирает use case с заданными адаптерами."""
        config = RunCollectionConfig(
            lease_seconds=60,
            heartbeat_seconds=int(kwargs.pop("heartbeat_seconds", 10_000)),
            batch_size=int(kwargs.pop("batch_size", 50)),
            idle_poll_seconds=0.01,
        )
        return RunCollection(
            collections=self.collections,
            documents=self.documents,
            adapter_runs=self.runs,
            adapters=adapters,
            classification=self.classification,
            clock=self.clock,
            config=config,
            unavailable_sources=kwargs.pop("unavailable_sources", None),  # type: ignore[arg-type]
        )


class HappyPathTest(RunCollectionTestBase):
    """Успешный сбор из двух источников."""

    async def test_documents_interleaved_and_counted(self) -> None:
        openalex = ScriptedAdapter(
            SourceKey.OPENALEX,
            [raw_document(f"https://dl.acm.org/doi/{i}", f"Публикация {i}") for i in range(3)],
            http_requests=2,
        )
        arxiv = ScriptedAdapter(
            SourceKey.ARXIV,
            [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(3)],
            http_requests=1,
        )
        collection = await self.make_collection((SourceKey.OPENALEX, SourceKey.ARXIV))
        status = await self.build({SourceKey.OPENALEX: openalex, SourceKey.ARXIV: arxiv}).execute(
            collection, OWNER
        )
        self.assertIs(status, OperationStatus.COMPLETED)
        self.assertEqual(collection.documents_total, 6)
        self.assertEqual(collection.http_requests_total, 3)
        sources = [item.draft.source_key for item in self.documents.persisted]
        self.assertEqual(
            sources,
            [SourceKey.OPENALEX, SourceKey.ARXIV] * 3,
            "документы должны чередоваться между адаптерами (round-robin)",
        )
        self.assertEqual([item.relevance_rank for item in self.documents.persisted], [1, 2, 3, 4, 5, 6])
        runs = {run.source_key: run for run in await self.runs.list_runs(collection.collection_id)}
        self.assertIs(runs[SourceKey.OPENALEX].status, OperationStatus.COMPLETED)
        self.assertEqual(runs[SourceKey.OPENALEX].documents_new, 3)
        self.assertEqual(runs[SourceKey.OPENALEX].http_requests, 2)
        self.assertEqual(runs[SourceKey.ARXIV].documents_found, 3)

    async def test_batches_limited_to_batch_size(self) -> None:
        documents = [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(120)]
        collection = await self.make_collection((SourceKey.ARXIV,))
        adapter = ScriptedAdapter(SourceKey.ARXIV, documents)
        status = await self.build({SourceKey.ARXIV: adapter}, batch_size=50).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.COMPLETED)
        self.assertTrue(all(size <= 50 for size in self.documents.batches))
        self.assertEqual(sum(self.documents.batches), 120)
        self.assertEqual(collection.documents_total, 120)

    async def test_document_classified_and_normalized(self) -> None:
        collection = await self.make_collection((SourceKey.RSS,))
        adapter = ScriptedAdapter(
            SourceKey.RSS,
            [raw_document("https://www.cnews.ru/news/1?utm_source=rss", "Нейроморфные чипы")],
        )
        await self.build({SourceKey.RSS: adapter}).execute(collection, OWNER)
        draft = self.documents.persisted[0].draft
        self.assertEqual(draft.canonical_url, "https://cnews.ru/news/1")
        self.assertEqual(draft.origin_domain, "cnews.ru")
        self.assertEqual(draft.source_type.value, "INDUSTRY_MEDIA")
        self.assertEqual(draft.trust_level.value, "MEDIUM")
        self.assertEqual(draft.language_code, "ru")


class DeduplicationTest(RunCollectionTestBase):
    """Дедупликация в пределах сбора."""

    async def test_same_url_from_two_adapters_stored_once(self) -> None:
        url = "https://dl.acm.org/doi/10.1145/1"
        first = ScriptedAdapter(SourceKey.OPENALEX, [raw_document(url, "Публикация")])
        second = ScriptedAdapter(SourceKey.ARXIV, [raw_document(f"{url}/", "Публикация")])
        collection = await self.make_collection((SourceKey.OPENALEX, SourceKey.ARXIV))
        await self.build({SourceKey.OPENALEX: first, SourceKey.ARXIV: second}).execute(collection, OWNER)
        self.assertEqual(collection.documents_total, 1)
        runs = {run.source_key: run for run in await self.runs.list_runs(collection.collection_id)}
        self.assertEqual(runs[SourceKey.ARXIV].documents_found, 1)
        self.assertEqual(runs[SourceKey.ARXIV].documents_new, 0)

    async def test_same_doi_deduplicated(self) -> None:
        first = ScriptedAdapter(
            SourceKey.OPENALEX, [raw_document("https://a.example/1", "Статья", doi="10.1/abc")]
        )
        second = ScriptedAdapter(
            SourceKey.ARXIV, [raw_document("https://b.example/2", "Другая ссылка", doi="10.1/ABC")]
        )
        collection = await self.make_collection((SourceKey.OPENALEX, SourceKey.ARXIV))
        await self.build({SourceKey.OPENALEX: first, SourceKey.ARXIV: second}).execute(collection, OWNER)
        self.assertEqual(collection.documents_total, 1)

    async def test_invalid_document_skipped(self) -> None:
        adapter = ScriptedAdapter(
            SourceKey.ARXIV,
            [raw_document("не-url", "Плохой"), raw_document("https://arxiv.org/abs/2", "Хороший")],
        )
        collection = await self.make_collection((SourceKey.ARXIV,))
        status = await self.build({SourceKey.ARXIV: adapter}).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.COMPLETED)
        self.assertEqual(collection.documents_total, 1)


class FailureTest(RunCollectionTestBase):
    """Отказы адаптеров и итоговые статусы."""

    async def test_one_adapter_failed_gives_partial(self) -> None:
        good = ScriptedAdapter(SourceKey.ARXIV, [raw_document("https://arxiv.org/abs/1", "Препринт")])
        bad = ScriptedAdapter(
            SourceKey.GITHUB,
            [],
            failure=AdapterFailure(AdapterErrorCode.HTTP_5XX.value, "источник ответил кодом 500"),
        )
        collection = await self.make_collection((SourceKey.ARXIV, SourceKey.GITHUB))
        status = await self.build({SourceKey.ARXIV: good, SourceKey.GITHUB: bad}).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.PARTIAL)
        self.assertEqual(collection.error_code, AdapterErrorCode.HTTP_5XX.value)
        runs = {run.source_key: run for run in await self.runs.list_runs(collection.collection_id)}
        self.assertIs(runs[SourceKey.GITHUB].status, OperationStatus.FAILED)
        self.assertIs(runs[SourceKey.ARXIV].status, OperationStatus.COMPLETED)

    async def test_all_adapters_failed_gives_failed(self) -> None:
        bad = ScriptedAdapter(
            SourceKey.ARXIV, [], failure=AdapterFailure(AdapterErrorCode.TIMEOUT.value, "таймаут")
        )
        collection = await self.make_collection((SourceKey.ARXIV,))
        status = await self.build({SourceKey.ARXIV: bad}).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.FAILED)
        self.assertEqual(collection.error_code, AdapterErrorCode.TIMEOUT.value)

    async def test_no_documents_gives_failed(self) -> None:
        empty = ScriptedAdapter(SourceKey.ARXIV, [])
        collection = await self.make_collection((SourceKey.ARXIV,))
        status = await self.build({SourceKey.ARXIV: empty}).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.FAILED)
        self.assertEqual(collection.error_code, "NO_DOCUMENTS")

    async def test_unexpected_adapter_exception_is_contained(self) -> None:
        broken = ScriptedAdapter(SourceKey.ARXIV, [], failure=RuntimeError("неожиданно"))
        good = ScriptedAdapter(SourceKey.OPENALEX, [raw_document("https://a.example/1", "Статья")])
        collection = await self.make_collection((SourceKey.ARXIV, SourceKey.OPENALEX))
        status = await self.build({SourceKey.ARXIV: broken, SourceKey.OPENALEX: good}).execute(
            collection, OWNER
        )
        self.assertIs(status, OperationStatus.PARTIAL)
        runs = {run.source_key: run for run in await self.runs.list_runs(collection.collection_id)}
        self.assertEqual(runs[SourceKey.ARXIV].error_code, AdapterErrorCode.PARSE_ERROR.value)

    async def test_unavailable_source_marked_without_adapter(self) -> None:
        good = ScriptedAdapter(SourceKey.ARXIV, [raw_document("https://arxiv.org/abs/1", "Препринт")])
        collection = await self.make_collection((SourceKey.ARXIV, SourceKey.OPENALEX))
        status = await self.build(
            {SourceKey.ARXIV: good},
            unavailable_sources={SourceKey.OPENALEX: AdapterErrorCode.AUTH_MISSING},
        ).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.PARTIAL)
        runs = {run.source_key: run for run in await self.runs.list_runs(collection.collection_id)}
        self.assertEqual(runs[SourceKey.OPENALEX].error_code, AdapterErrorCode.AUTH_MISSING.value)
        self.assertIs(runs[SourceKey.OPENALEX].status, OperationStatus.FAILED)


class LimitsTest(RunCollectionTestBase):
    """Лимиты сбора: общий объём, объём на источник, бюджет времени."""

    async def test_total_cap_stops_collection(self) -> None:
        documents = [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(40)]
        collection = await self.make_collection(
            (SourceKey.ARXIV,), limits=CollectionLimits(150, 5, 120, 2023)
        )
        adapter = ScriptedAdapter(SourceKey.ARXIV, documents)
        status = await self.build({SourceKey.ARXIV: adapter}, batch_size=5).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.COMPLETED)
        self.assertEqual(collection.documents_total, 5)

    async def test_per_source_cap(self) -> None:
        first = ScriptedAdapter(
            SourceKey.ARXIV,
            [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(5)],
        )
        second = ScriptedAdapter(
            SourceKey.OPENALEX,
            [raw_document(f"https://a.example/{i}", f"Статья {i}") for i in range(5)],
        )
        collection = await self.make_collection(
            (SourceKey.ARXIV, SourceKey.OPENALEX), limits=CollectionLimits(2, 800, 120, 2023)
        )
        await self.build({SourceKey.ARXIV: first, SourceKey.OPENALEX: second}).execute(collection, OWNER)
        self.assertEqual(collection.documents_total, 4)

    async def test_budget_exhausted_gives_partial(self) -> None:
        documents = [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(30)]
        adapter = ScriptedAdapter(SourceKey.ARXIV, documents, clock=self.clock, advance_seconds=3.0)
        collection = await self.make_collection(
            (SourceKey.ARXIV,), limits=CollectionLimits(150, 800, 10, 2023)
        )
        status = await self.build({SourceKey.ARXIV: adapter}, batch_size=2).execute(collection, OWNER)
        self.assertIs(status, OperationStatus.PARTIAL)
        self.assertEqual(collection.error_code, AdapterErrorCode.BUDGET_EXHAUSTED.value)
        self.assertLess(collection.documents_total, 30)
        runs = await self.runs.list_runs(collection.collection_id)
        self.assertEqual(runs[0].error_code, AdapterErrorCode.BUDGET_EXHAUSTED.value)
        self.assertIs(runs[0].status, OperationStatus.COMPLETED)


class CancellationAndLeaseTest(RunCollectionTestBase):
    """Кооперативная отмена и потеря аренды."""

    async def test_cancel_requested_gives_cancelled(self) -> None:
        documents = [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(200)]
        adapter = ScriptedAdapter(SourceKey.ARXIV, documents)
        collection = await self.make_collection(
            (SourceKey.ARXIV,), limits=CollectionLimits(500, 3000, 600, 2023)
        )
        collection.cancel_requested = True  # отмена пришла через CancelCollection
        status = await self.build({SourceKey.ARXIV: adapter}, heartbeat_seconds=1, batch_size=5).execute(
            collection, OWNER
        )
        self.assertIs(status, OperationStatus.CANCELLED)
        self.assertEqual(collection.error_code, "CANCELLED")
        runs = await self.runs.list_runs(collection.collection_id)
        self.assertIs(runs[0].status, OperationStatus.CANCELLED)

    async def test_lease_lost_stops_without_finishing(self) -> None:
        documents = [raw_document(f"https://arxiv.org/abs/{i}", f"Препринт {i}") for i in range(200)]
        adapter = ScriptedAdapter(SourceKey.ARXIV, documents)
        collection = await self.make_collection(
            (SourceKey.ARXIV,), limits=CollectionLimits(500, 3000, 600, 2023)
        )
        use_case = self.build({SourceKey.ARXIV: adapter}, heartbeat_seconds=1, batch_size=5)
        collection.status = OperationStatus.PENDING  # аренду перехватил другой воркер
        with self.assertRaises(LeaseLost):
            await use_case.execute(collection, OWNER)
        self.assertIs(collection.status, OperationStatus.PENDING)
        self.assertEqual(collection.finished_at, None)

    async def test_resume_continues_rank_numbering(self) -> None:
        collection = await self.make_collection((SourceKey.ARXIV,))
        collection.documents_total = 10  # документы предыдущей попытки
        adapter = ScriptedAdapter(SourceKey.ARXIV, [raw_document("https://arxiv.org/abs/x", "Препринт")])
        await self.build({SourceKey.ARXIV: adapter}).execute(collection, OWNER)
        self.assertEqual(self.documents.persisted[0].relevance_rank, 11)


if __name__ == "__main__":
    unittest.main()
