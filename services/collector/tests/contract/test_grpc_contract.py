"""Все 6 RPC `CollectorService` через настоящий gRPC-транспорт (сериализация, статусы, метаданные).

Запуск: `uv run pytest services/collector/tests/contract -m contract` (нужны grpcio и ws_contracts).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
import pytest_asyncio
from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc
from weaksignals.common.v1 import common_pb2

from collector.adapters.inbound.grpc_server import CollectorServicer
from collector.adapters.outbound.rules_loader import load_classification_config
from collector.application.dto import BatchItem, EncyclopediaHit
from collector.application.use_cases.cancel_collection import CancelCollection
from collector.application.use_cases.check_encyclopedia import CheckEncyclopedia
from collector.application.use_cases.get_collection import GetCollection
from collector.application.use_cases.get_documents import GetDocuments
from collector.application.use_cases.start_collection import StartCollection
from collector.application.use_cases.stream_documents import StreamDocuments
from collector.domain.normalization import build_document_draft
from collector.domain.values import OperationStatus, SourceKey
from ws_common.grpc_interceptors import ObservabilityInterceptor

from ..fakes import (
    BASE_TIME,
    FakeClock,
    InMemoryAdapterRunRepository,
    InMemoryCollectionRepository,
    InMemoryDocumentRepository,
    InMemoryEncyclopediaCache,
    raw_document,
)

pytestmark = pytest.mark.contract

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SOURCES = (SourceKey.OPENALEX, SourceKey.ARXIV)


class StubProbe:
    """Тестовая замена внешней проверки Wikipedia."""

    http_requests = 0

    async def probe(self, title: str, language_code: str) -> EncyclopediaHit:
        """Возвращает предсказуемый результат без сети."""
        return EncyclopediaHit(
            title=title,
            exists=True,
            page_url=f"https://{language_code}.wikipedia.org/wiki/{title}",
            pageviews_30d=1234,
        )


class Context:
    """Собранный стенд: сервер, канал и тестовые хранилища."""

    def __init__(self, stub, collections, runs, documents) -> None:  # noqa: ANN001 - стенд тестов
        self.stub = stub
        self.collections = collections
        self.runs = runs
        self.documents = documents


@pytest_asyncio.fixture
async def context() -> AsyncIterator[Context]:
    """Поднимает gRPC-сервер на свободном порту с in-memory портами."""
    clock = FakeClock()
    runs = InMemoryAdapterRunRepository()
    collections = InMemoryCollectionRepository(clock, runs)
    documents = InMemoryDocumentRepository(clock, collections)
    cache = InMemoryEncyclopediaCache()
    servicer = CollectorServicer(
        start_collection=StartCollection(collections, SOURCES, max_pending=2),
        get_collection=GetCollection(collections, runs),
        stream_documents=StreamDocuments(collections, documents),
        get_documents=GetDocuments(documents),
        cancel_collection=CancelCollection(collections),
        check_encyclopedia=CheckEncyclopedia(cache, StubProbe(), clock, cache_days=7),
    )
    server = grpc.aio.server(interceptors=[ObservabilityInterceptor("collector-test")])
    collector_pb2_grpc.add_CollectorServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield Context(collector_pb2_grpc.CollectorServiceStub(channel), collections, runs, documents)
    await server.stop(0)


def start_request(key: str = "job-0001:collect", **overrides) -> collector_pb2.StartCollectionRequest:  # noqa: ANN003
    """Корректный запрос запуска сбора."""
    request = collector_pb2.StartCollectionRequest(
        idempotency_key=key,
        query_text="технологии защиты ИИ-систем",
        terms=collector_pb2.SearchTerms(ru=["защита ИИ"], en=["ai security"]),
        mode=collector_pb2.COLLECTION_MODE_SEARCH,
    )
    for field, value in overrides.items():
        setattr(request, field, value)
    return request


async def test_start_collection_is_idempotent(context: Context) -> None:
    """Повтор с тем же ключом возвращает ту же коллекцию с `already_existed=true`."""
    first = await context.stub.StartCollection(start_request(), metadata=(("x-correlation-id", "test-1"),))
    second = await context.stub.StartCollection(start_request(), metadata=(("x-correlation-id", "test-1"),))
    assert first.already_existed is False
    assert second.already_existed is True
    assert first.collection_id == second.collection_id
    assert first.status == common_pb2.OPERATION_STATUS_PENDING


async def test_validation_errors_carry_error_code(context: Context) -> None:
    """Нарушение правил §10.1 → INVALID_ARGUMENT и `error_code` в trailing metadata."""
    cases = [
        (start_request(key="short"), "INVALID_IDEMPOTENCY_KEY"),
        (start_request(query_text=" "), "INVALID_QUERY"),
        (
            collector_pb2.StartCollectionRequest(
                idempotency_key="job-0002:collect",
                query_text="запрос",
                terms=collector_pb2.SearchTerms(),
                mode=collector_pb2.COLLECTION_MODE_SEARCH,
            ),
            "INVALID_TERMS",
        ),
        (
            collector_pb2.StartCollectionRequest(
                idempotency_key="job-0003:collect",
                query_text="запрос",
                terms=collector_pb2.SearchTerms(ru=["защита ИИ"]),
                mode=collector_pb2.COLLECTION_MODE_UNSPECIFIED,
            ),
            "INVALID_ENUM",
        ),
    ]
    for request, expected_code in cases:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await context.stub.StartCollection(request)
        assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        codes = {key: value for key, value in error.value.trailing_metadata()}
        assert codes.get("error_code") == expected_code


async def test_resource_exhausted_when_queue_full(context: Context) -> None:
    """Переполнение очереди → RESOURCE_EXHAUSTED `QUEUE_FULL`."""
    await context.stub.StartCollection(start_request("job-0001:collect"))
    await context.stub.StartCollection(start_request("job-0002:collect"))
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await context.stub.StartCollection(start_request("job-0003:collect"))
    assert error.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED


async def test_get_collection_not_found_and_invalid_uuid(context: Context) -> None:
    """NOT_FOUND для несуществующей коллекции, INVALID_ARGUMENT для неверного UUID."""
    with pytest.raises(grpc.aio.AioRpcError) as invalid:
        await context.stub.GetCollection(collector_pb2.GetCollectionRequest(collection_id="нет"))
    assert invalid.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as missing:
        await context.stub.GetCollection(
            collector_pb2.GetCollectionRequest(collection_id="00000000-0000-4000-8000-000000009999")
        )
    assert missing.value.code() is grpc.StatusCode.NOT_FOUND


async def test_stream_requires_terminal_collection(context: Context) -> None:
    """Стрим по незавершённой коллекции → FAILED_PRECONDITION `COLLECTION_NOT_TERMINAL`."""
    started = await context.stub.StartCollection(start_request())
    stream = context.stub.StreamDocuments(
        collector_pb2.StreamDocumentsRequest(collection_id=started.collection_id)
    )
    with pytest.raises(grpc.aio.AioRpcError) as error:
        async for _ in stream:
            pass
    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION


async def test_stream_chunks_and_resume(context: Context) -> None:
    """Документы отдаются чанками и возобновляются по `next_page_token`."""
    started = await context.stub.StartCollection(start_request())
    classification = load_classification_config(
        CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml"
    )
    items = [
        BatchItem(
            draft=build_document_draft(
                raw_document(f"https://arxiv.org/abs/{index}", f"Препринт {index}"),
                source_key=SourceKey.ARXIV,
                config=classification,
                raw_meta_keys=frozenset(),
                fetched_at=BASE_TIME,
            ),
            relevance_rank=index + 1,
        )
        for index in range(5)
    ]
    await context.documents.persist_batch(started.collection_id, items, {})
    collection = context.collections.items[started.collection_id]
    collection.transition_to(OperationStatus.RUNNING, BASE_TIME)
    collection.finish(OperationStatus.COMPLETED, BASE_TIME)

    chunks = [
        chunk
        async for chunk in context.stub.StreamDocuments(
            collector_pb2.StreamDocumentsRequest(collection_id=started.collection_id, chunk_size=2)
        )
    ]
    assert [len(chunk.documents) for chunk in chunks] == [2, 2, 1]
    assert chunks[-1].last is True
    document = chunks[0].documents[0]
    assert document.source_key == common_pb2.SOURCE_KEY_ARXIV
    assert document.trust_level != common_pb2.TRUST_LEVEL_UNSPECIFIED
    assert document.HasField("published_at")

    resumed = [
        chunk
        async for chunk in context.stub.StreamDocuments(
            collector_pb2.StreamDocumentsRequest(
                collection_id=started.collection_id,
                chunk_size=2,
                page_token=chunks[0].next_page_token,
            )
        )
    ]
    assert [len(chunk.documents) for chunk in resumed] == [2, 1]

    with pytest.raises(grpc.aio.AioRpcError) as error:
        async for _ in context.stub.StreamDocuments(
            collector_pb2.StreamDocumentsRequest(
                collection_id=started.collection_id, page_token="подделка"
            )
        ):
            pass
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_get_documents_reports_missing(context: Context) -> None:
    """Отсутствующие идентификаторы возвращаются отдельным списком."""
    missing_id = "11111111-0000-4000-8000-000000009999"
    response = await context.stub.GetDocuments(
        collector_pb2.GetDocumentsRequest(document_ids=[missing_id])
    )
    assert list(response.missing_document_ids) == [missing_id]
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await context.stub.GetDocuments(collector_pb2.GetDocumentsRequest(document_ids=[]))
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_cancel_is_idempotent(context: Context) -> None:
    """Повторная отмена возвращает фактический статус без ошибки."""
    started = await context.stub.StartCollection(start_request())
    first = await context.stub.CancelCollection(
        collector_pb2.CancelCollectionRequest(collection_id=started.collection_id, reason="не нужно")
    )
    second = await context.stub.CancelCollection(
        collector_pb2.CancelCollectionRequest(collection_id=started.collection_id)
    )
    assert first.status == common_pb2.OPERATION_STATUS_CANCELLED
    assert second.status == common_pb2.OPERATION_STATUS_CANCELLED


async def test_check_encyclopedia_order_and_validation(context: Context) -> None:
    """Ответы идут в порядке запроса; неверный язык отклоняется."""
    response = await context.stub.CheckEncyclopedia(
        collector_pb2.CheckEncyclopediaRequest(titles=["Kubernetes", "Ray"], language_code="en")
    )
    assert [hit.title for hit in response.hits] == ["Kubernetes", "Ray"]
    assert response.hits[0].pageviews_30d == 1234
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await context.stub.CheckEncyclopedia(
            collector_pb2.CheckEncyclopediaRequest(titles=["Kubernetes"], language_code="de")
        )
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_get_collection_returns_adapter_runs(context: Context) -> None:
    """`GetCollection` отдаёт сводку по запускам адаптеров."""
    started = await context.stub.StartCollection(start_request())
    response = await context.stub.GetCollection(
        collector_pb2.GetCollectionRequest(collection_id=started.collection_id)
    )
    assert {run.source_key for run in response.adapter_runs} == {
        common_pb2.SOURCE_KEY_OPENALEX,
        common_pb2.SOURCE_KEY_ARXIV,
    }
    assert response.status == common_pb2.OPERATION_STATUS_PENDING
