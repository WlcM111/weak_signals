"""Прикладные сценарии на тестовых заменах портов (in-memory)."""

from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.application.dto import EncyclopediaHit, StartCollectionCommand
from collector.application.use_cases.cancel_collection import CancelCollection
from collector.application.use_cases.check_encyclopedia import CheckEncyclopedia
from collector.application.use_cases.get_collection import GetCollection
from collector.application.use_cases.get_documents import GetDocuments
from collector.application.use_cases.purge_documents import PurgeDocuments
from collector.application.use_cases.start_collection import StartCollection
from collector.application.use_cases.stream_documents import StreamDocuments
from collector.domain.errors import NotFoundError, PreconditionFailedError, ResourceExhaustedError
from collector.domain.rules import decode_page_token
from collector.domain.values import (
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
)

from ..fakes import (
    BASE_TIME,
    FakeClock,
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    InMemoryEncyclopediaCache,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SOURCES = (SourceKey.OPENALEX, SourceKey.ARXIV)


def command(key: str = "job-0001:collect", **overrides: object) -> StartCollectionCommand:
    """Команда запуска сбора со значениями по умолчанию."""
    fields = {
        "idempotency_key": key,
        "query_text": "технологии защиты ИИ-систем",
        "terms": SearchTerms(ru=("защита ИИ",), en=("ai security",)),
        "mode": CollectionMode.SEARCH,
        "limits": CollectionLimits.defaults(CollectionMode.SEARCH),
        "sources": (),
    }
    fields.update(overrides)
    return StartCollectionCommand(**fields)  # type: ignore[arg-type]


class StartCollectionTest(unittest.IsolatedAsyncioTestCase):
    """Идемпотентность, выбор источников и защита очереди."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.runs = InMemoryAdapterRunRepository()
        self.collections = InMemoryCollectionRepository(self.clock, self.runs)
        self.use_case = StartCollection(self.collections, SOURCES, max_pending=2)

    async def test_creates_collection_with_runs(self) -> None:
        result = await self.use_case.execute(command())
        self.assertFalse(result.already_existed)
        self.assertIs(result.status, OperationStatus.PENDING)
        self.assertEqual(
            {run.source_key for run in self.runs.runs[result.collection_id].values()}, set(SOURCES)
        )

    async def test_repeated_key_returns_same_collection(self) -> None:
        first = await self.use_case.execute(command())
        second = await self.use_case.execute(command())
        self.assertEqual(first.collection_id, second.collection_id)
        self.assertTrue(second.already_existed)
        self.assertEqual(len(self.collections.items), 1)

    async def test_explicit_sources_are_used(self) -> None:
        result = await self.use_case.execute(command(sources=(SourceKey.GITHUB,)))
        self.assertEqual(
            {run.source_key for run in self.runs.runs[result.collection_id].values()},
            {SourceKey.GITHUB},
        )

    async def test_queue_limit(self) -> None:
        await self.use_case.execute(command("job-0001:collect"))
        await self.use_case.execute(command("job-0002:collect"))
        with self.assertRaises(ResourceExhaustedError) as ctx:
            await self.use_case.execute(command("job-0003:collect"))
        self.assertEqual(ctx.exception.error_code, "QUEUE_FULL")

    async def test_no_available_sources(self) -> None:
        use_case = StartCollection(self.collections, (), max_pending=10)
        with self.assertRaises(ResourceExhaustedError) as ctx:
            await use_case.execute(command())
        self.assertEqual(ctx.exception.error_code, "NO_SOURCES_ENABLED")


class ReadUseCasesTest(unittest.IsolatedAsyncioTestCase):
    """GetCollection, GetDocuments, CancelCollection."""

    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.runs = InMemoryAdapterRunRepository()
        self.collections = InMemoryCollectionRepository(self.clock, self.runs)
        self.documents = InMemoryDocumentRepository(self.clock, self.collections)
        self.start = StartCollection(self.collections, SOURCES, max_pending=10)
        self.result = await self.start.execute(command())

    async def test_get_collection_view(self) -> None:
        view = await GetCollection(self.collections, self.runs).execute(self.result.collection_id)
        self.assertIs(view.status, OperationStatus.PENDING)
        self.assertEqual(len(view.adapter_runs), 2)
        self.assertEqual(view.sources_processed, 0)

    async def test_get_collection_missing(self) -> None:
        with self.assertRaises(NotFoundError):
            await GetCollection(self.collections, self.runs).execute("00000000-0000-4000-8000-000000000999")

    async def test_get_documents_reports_missing(self) -> None:
        missing_id = "11111111-0000-4000-8000-000000009999"
        result = await GetDocuments(self.documents).execute([missing_id])
        self.assertEqual(result.documents, ())
        self.assertEqual(result.missing_document_ids, (missing_id,))

    async def test_cancel_is_idempotent(self) -> None:
        use_case = CancelCollection(self.collections)
        first = await use_case.execute(self.result.collection_id, "не нужно")
        second = await use_case.execute(self.result.collection_id, "повтор")
        self.assertIs(first, OperationStatus.CANCELLED)
        self.assertIs(second, OperationStatus.CANCELLED)

    async def test_cancel_missing_collection(self) -> None:
        with self.assertRaises(NotFoundError):
            await CancelCollection(self.collections).execute("00000000-0000-4000-8000-000000000999", "")


class StreamDocumentsTest(unittest.IsolatedAsyncioTestCase):
    """Поток документов: предусловие терминального статуса, чанки, возобновление."""

    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.runs = InMemoryAdapterRunRepository()
        self.collections = InMemoryCollectionRepository(self.clock, self.runs)
        self.documents = InMemoryDocumentRepository(self.clock, self.collections)
        self.classification = load_classification_config(
            CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml"
        )
        result = await StartCollection(self.collections, SOURCES, max_pending=10).execute(command())
        self.collection_id = result.collection_id
        await self._seed_documents(7)
        self.use_case = StreamDocuments(self.collections, self.documents)

    async def _seed_documents(self, count: int) -> None:
        """Заполняет коллекцию документами через реальную нормализацию."""
        from collector.application.dto import BatchItem
        from collector.domain.normalization import build_document_draft

        from ..fakes import raw_document

        items = [
            BatchItem(
                draft=build_document_draft(
                    raw_document(f"https://arxiv.org/abs/{index}", title=f"Документ {index}"),
                    source_key=SourceKey.ARXIV,
                    config=self.classification,
                    raw_meta_keys=frozenset(),
                    fetched_at=BASE_TIME,
                ),
                relevance_rank=index + 1,
            )
            for index in range(count)
        ]
        await self.documents.persist_batch(self.collection_id, items, {})

    async def test_requires_terminal_status(self) -> None:
        with self.assertRaises(PreconditionFailedError) as ctx:
            [page async for page in self.use_case.execute(self.collection_id, 50, None)]
        self.assertEqual(ctx.exception.error_code, "COLLECTION_NOT_TERMINAL")

    async def _finish(self) -> None:
        collection = self.collections.items[self.collection_id]
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        collection.finish(OperationStatus.COMPLETED, self.clock.now())

    async def test_chunks_and_last_flag(self) -> None:
        await self._finish()
        pages = [page async for page in self.use_case.execute(self.collection_id, 3, None)]
        self.assertEqual([len(page.documents) for page in pages], [3, 3, 1])
        self.assertTrue(pages[-1].last)
        self.assertFalse(pages[0].last)
        self.assertEqual(pages[-1].next_page_token, "")

    async def test_resume_from_page_token(self) -> None:
        await self._finish()
        pages = [page async for page in self.use_case.execute(self.collection_id, 3, None)]
        token = pages[0].next_page_token
        resumed = [
            page async for page in self.use_case.execute(self.collection_id, 3, decode_page_token(token))
        ]
        self.assertEqual([len(page.documents) for page in resumed], [3, 1])
        first_titles = {document.title for document in pages[1].documents}
        self.assertEqual(first_titles, {document.title for document in resumed[0].documents})

    async def test_empty_collection_yields_last_chunk(self) -> None:
        result = await StartCollection(self.collections, SOURCES, max_pending=10).execute(
            command("job-0002:collect")
        )
        collection = self.collections.items[result.collection_id]
        collection.transition_to(OperationStatus.RUNNING, self.clock.now())
        collection.finish(OperationStatus.FAILED, self.clock.now(), "NO_DOCUMENTS", "нет документов")
        pages = [page async for page in self.use_case.execute(result.collection_id, 50, None)]
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].last)
        self.assertEqual(pages[0].documents, ())

    async def test_unknown_collection(self) -> None:
        with self.assertRaises(NotFoundError):
            [page async for page in self.use_case.execute("00000000-0000-4000-8000-000000000999", 50, None)]


class CheckEncyclopediaTest(unittest.IsolatedAsyncioTestCase):
    """Кеш проверок Wikipedia и порядок ответов."""

    class StubProbe:
        """Заглушка внешней проверки: считает обращения."""

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.http_requests = 0

        async def probe(self, title: str, language_code: str) -> EncyclopediaHit:
            self.calls.append(title)
            self.http_requests += 1
            return EncyclopediaHit(
                title=title,
                exists=title != "Отсутствующая",
                page_url=f"https://{language_code}.wikipedia.org/wiki/{title}",
                pageviews_30d=1000,
            )

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.cache = InMemoryEncyclopediaCache()
        self.probe = self.StubProbe()
        self.use_case = CheckEncyclopedia(self.cache, self.probe, self.clock, cache_days=7)

    async def test_first_call_queries_probe_and_caches(self) -> None:
        hits = await self.use_case.execute(["Kubernetes", "Отсутствующая"], "en")
        self.assertEqual([hit.title for hit in hits], ["Kubernetes", "Отсутствующая"])
        self.assertTrue(hits[0].exists)
        self.assertFalse(hits[1].exists)
        self.assertEqual(self.cache.writes, 2)

    async def test_second_call_uses_cache(self) -> None:
        await self.use_case.execute(["Kubernetes"], "en")
        await self.use_case.execute(["kubernetes  "], "en")
        self.assertEqual(len(self.probe.calls), 1)

    async def test_expired_cache_is_refreshed(self) -> None:
        await self.use_case.execute(["Kubernetes"], "en")
        self.clock.advance(timedelta(days=8).total_seconds())
        await self.use_case.execute(["Kubernetes"], "en")
        self.assertEqual(len(self.probe.calls), 2)

    async def test_duplicate_titles_in_one_request(self) -> None:
        hits = await self.use_case.execute(["Kubernetes", "Kubernetes"], "en")
        self.assertEqual(len(hits), 2)
        self.assertEqual(len(self.probe.calls), 1)


class PurgeDocumentsTest(unittest.IsolatedAsyncioTestCase):
    """Ретенция документов и кеша."""

    async def test_purge_removes_orphans_and_stale_cache(self) -> None:
        clock = FakeClock()
        runs = InMemoryAdapterRunRepository()
        collections = InMemoryCollectionRepository(clock, runs)
        documents = InMemoryDocumentRepository(clock, collections)
        cache = InMemoryEncyclopediaCache()
        await cache.put("en", "kubernetes", EncyclopediaHit(title="Kubernetes", exists=True))
        clock.advance(timedelta(days=31).total_seconds())
        result = await PurgeDocuments(documents, cache, clock, retention_days=90).execute()
        self.assertEqual(result.cache_entries_deleted, 1)
        self.assertEqual(result.documents_deleted, 0)


if __name__ == "__main__":
    unittest.main()
