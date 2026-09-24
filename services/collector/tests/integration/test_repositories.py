"""Репозитории PostgreSQL: миграции, дедупликация, транзакции, аренды, ретенция.

Запуск: `uv run pytest services/collector/tests/integration -m integration` (нужен Docker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collector.adapters.outbound.postgres.adapter_run_repository import PostgresAdapterRunRepository
from collector.adapters.outbound.postgres.collection_repository import PostgresCollectionRepository
from collector.adapters.outbound.postgres.document_repository import PostgresDocumentRepository
from collector.adapters.outbound.postgres.encyclopedia_cache import PostgresEncyclopediaCache
from collector.adapters.outbound.postgres.source_catalog import PostgresSourceCatalog
from collector.application.dto import AdapterDelta, BatchItem, CollectionDraft, EncyclopediaHit
from collector.domain.entities import AdapterRun, DocumentDraft
from collector.domain.values import (
    AdapterErrorCode,
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
    SourceType,
    TrustLevel,
)
from ws_common.migrate import apply_migrations

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def draft(url: str, *, doi: str | None = None, source: SourceKey = SourceKey.ARXIV) -> DocumentDraft:
    """Черновик документа для записи."""
    return DocumentDraft(
        url=url,
        canonical_url=url,
        url_hash=f"hash-{url}",
        origin_domain="arxiv.org",
        title=f"Документ {url}",
        text="Аннотация",
        content_hash=f"content-{url}",
        language_code="ru",
        source_key=source,
        source_type=SourceType.PREPRINT,
        trust_level=TrustLevel.HIGH,
        fetched_at=NOW,
        matched_term="защита ИИ",
        doi=doi,
        raw_meta={"categories": ["cs.CR"]},
    )


def collection_draft(key: str = "job-1:collect") -> CollectionDraft:
    """Данные новой коллекции."""
    return CollectionDraft(
        idempotency_key=key,
        query_text="технологии защиты ИИ-систем",
        mode=CollectionMode.SEARCH,
        terms=SearchTerms(ru=("защита ИИ",), en=("ai security",)),
        limits=CollectionLimits.defaults(CollectionMode.SEARCH),
        sources=(SourceKey.ARXIV, SourceKey.OPENALEX),
    )


async def test_migration_is_idempotent(pool) -> None:  # noqa: ANN001 - фикстура pytest
    """Повторный запуск runner-а не применяет миграцию второй раз."""
    from pathlib import Path

    applied = await apply_migrations(pool, "collector", Path(__file__).resolve().parents[2] / "migrations")
    assert applied == []


async def test_source_catalog_seeded(pool) -> None:  # noqa: ANN001
    """Seed каталога адаптеров присутствует после миграции."""
    catalog = await PostgresSourceCatalog(pool).load()
    assert set(catalog) == set(SourceKey)
    assert catalog[SourceKey.OPENALEX].requires_api_key is True
    assert catalog[SourceKey.HH].enabled is False


async def test_create_if_absent_is_idempotent(pool) -> None:  # noqa: ANN001
    """Повтор по `idempotency_key` возвращает ту же коллекцию и не дублирует запуски."""
    repository = PostgresCollectionRepository(pool)
    first, existed_first = await repository.create_if_absent(collection_draft())
    second, existed_second = await repository.create_if_absent(collection_draft())
    assert existed_first is False
    assert existed_second is True
    assert first.collection_id == second.collection_id
    runs = await PostgresAdapterRunRepository(pool).list_runs(first.collection_id)
    assert {run.source_key for run in runs} == {SourceKey.ARXIV, SourceKey.OPENALEX}
    assert all(run.status is OperationStatus.PENDING for run in runs)


async def test_persist_batch_deduplicates_and_counts(pool) -> None:  # noqa: ANN001
    """Один документ по `url_hash`, связь с коллекцией и счётчики — в одной транзакции."""
    collections = PostgresCollectionRepository(pool)
    documents = PostgresDocumentRepository(pool)
    collection, _ = await collections.create_if_absent(collection_draft())
    items = [
        BatchItem(draft=draft("https://arxiv.org/abs/1"), relevance_rank=1),
        BatchItem(draft=draft("https://arxiv.org/abs/2"), relevance_rank=2),
    ]
    outcome = await documents.persist_batch(
        collection.collection_id, items, {SourceKey.ARXIV: AdapterDelta(http_requests=3, documents_found=2)}
    )
    assert outcome.new_documents == 2
    assert outcome.documents_total == 2
    repeat = await documents.persist_batch(collection.collection_id, items, {})
    assert repeat.new_documents == 0
    assert repeat.documents_total == 2
    stored = await collections.get(collection.collection_id)
    assert stored is not None
    assert stored.http_requests_total == 3
    runs = {run.source_key: run for run in await PostgresAdapterRunRepository(pool).list_runs(collection.collection_id)}
    assert runs[SourceKey.ARXIV].documents_found == 2
    assert runs[SourceKey.ARXIV].documents_new == 2


async def test_doi_links_to_existing_document(pool) -> None:  # noqa: ANN001
    """Совпадение DOI связывает коллекцию с существующим документом без создания нового."""
    collections = PostgresCollectionRepository(pool)
    documents = PostgresDocumentRepository(pool)
    collection, _ = await collections.create_if_absent(collection_draft())
    await documents.persist_batch(
        collection.collection_id,
        [BatchItem(draft=draft("https://arxiv.org/abs/1", doi="10.1/a"), relevance_rank=1)],
        {},
    )
    outcome = await documents.persist_batch(
        collection.collection_id,
        [BatchItem(draft=draft("https://other.example/2", doi="10.1/a"), relevance_rank=2)],
        {},
    )
    assert outcome.new_documents == 0
    assert outcome.linked_documents == 0  # документ уже связан с этой коллекцией


async def test_lease_lifecycle(pool) -> None:  # noqa: ANN001
    """Захват, heartbeat, истечение аренды и завершение владельцем."""
    repository = PostgresCollectionRepository(pool)
    collection, _ = await repository.create_if_absent(collection_draft())
    claimed = await repository.claim_next("worker-1", lease_seconds=60)
    assert claimed is not None
    assert claimed.status is OperationStatus.RUNNING
    assert await repository.claim_next("worker-2", lease_seconds=60) is None
    alive = await repository.heartbeat(claimed.collection_id, "worker-1", 60)
    assert alive.alive is True
    foreign = await repository.heartbeat(claimed.collection_id, "worker-2", 60)
    assert foreign.alive is False
    assert await repository.finish(
        claimed.collection_id,
        "worker-2",
        OperationStatus.COMPLETED,
        error_code="",
        error_message="",
        finished_at=NOW,
    ) is False
    assert await repository.finish(
        claimed.collection_id,
        "worker-1",
        OperationStatus.COMPLETED,
        error_code="",
        error_message="",
        finished_at=NOW,
    ) is True


async def test_expired_lease_returns_to_queue(pool) -> None:  # noqa: ANN001
    """Коллекция с истёкшей арендой возвращается в PENDING."""
    repository = PostgresCollectionRepository(pool)
    collection, _ = await repository.create_if_absent(collection_draft())
    claimed = await repository.claim_next("worker-1", lease_seconds=1)
    assert claimed is not None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE collections SET lease_expires_at = now() - interval '1 minute' WHERE collection_id = %s",
            (claimed.collection_id,),
        )
    assert await repository.release_expired_leases() == 1
    again = await repository.claim_next("worker-2", lease_seconds=60)
    assert again is not None and again.collection_id == claimed.collection_id


async def test_cancel_pending_and_running(pool) -> None:  # noqa: ANN001
    """PENDING отменяется сразу, RUNNING получает признак отмены."""
    repository = PostgresCollectionRepository(pool)
    pending, _ = await repository.create_if_absent(collection_draft("job-pending:collect"))
    assert await repository.request_cancel(pending.collection_id) is OperationStatus.CANCELLED
    running, _ = await repository.create_if_absent(collection_draft("job-running:collect"))
    claimed = await repository.claim_next("worker-1", lease_seconds=60)
    assert claimed is not None and claimed.collection_id == running.collection_id
    assert await repository.request_cancel(running.collection_id) is OperationStatus.RUNNING
    lease = await repository.heartbeat(running.collection_id, "worker-1", 60)
    assert lease.cancel_requested is True


async def test_keyset_pagination(pool) -> None:  # noqa: ANN001
    """Страницы документов идут по `(relevance_rank, document_id)` без пропусков."""
    collections = PostgresCollectionRepository(pool)
    documents = PostgresDocumentRepository(pool)
    collection, _ = await collections.create_if_absent(collection_draft())
    await documents.persist_batch(
        collection.collection_id,
        [BatchItem(draft=draft(f"https://arxiv.org/abs/{index}"), relevance_rank=index) for index in range(1, 8)],
        {},
    )
    first = await documents.page(collection.collection_id, None, 3)
    assert [rank for rank, _ in first] == [1, 2, 3]
    cursor = (first[-1][0], first[-1][1].document_id)
    second = await documents.page(collection.collection_id, cursor, 3)
    assert [rank for rank, _ in second] == [4, 5, 6]


async def test_adapter_run_upsert(pool) -> None:  # noqa: ANN001
    """Повторное сохранение запуска обновляет строку, не создавая вторую."""
    collections = PostgresCollectionRepository(pool)
    runs = PostgresAdapterRunRepository(pool)
    collection, _ = await collections.create_if_absent(collection_draft())
    run = AdapterRun(source_key=SourceKey.ARXIV, status=OperationStatus.RUNNING, started_at=NOW)
    await runs.save(collection.collection_id, run)
    run.fail(NOW, AdapterErrorCode.HTTP_5XX, "источник ответил кодом 500")
    await runs.save(collection.collection_id, run)
    stored = {item.source_key: item for item in await runs.list_runs(collection.collection_id)}
    assert stored[SourceKey.ARXIV].status is OperationStatus.FAILED
    assert stored[SourceKey.ARXIV].error_code == AdapterErrorCode.HTTP_5XX.value


async def test_encyclopedia_cache_ttl(pool) -> None:  # noqa: ANN001
    """Кеш отдаёт только свежие записи и очищается по дате."""
    cache = PostgresEncyclopediaCache(pool)
    await cache.put("en", "kubernetes", EncyclopediaHit(title="Kubernetes", exists=True, pageviews_30d=5))
    fresh = await cache.get_many("en", ["kubernetes"], datetime.now(UTC) - timedelta(days=7))
    assert fresh["kubernetes"].exists is True
    stale = await cache.get_many("en", ["kubernetes"], datetime.now(UTC) + timedelta(days=1))
    assert stale == {}
    assert await cache.purge_older_than(datetime.now(UTC) + timedelta(days=1)) == 1


async def test_retention_removes_orphans(pool) -> None:  # noqa: ANN001
    """Документы вне актуальных коллекций удаляются батчами."""
    documents = PostgresDocumentRepository(pool)
    collections = PostgresCollectionRepository(pool)
    collection, _ = await collections.create_if_absent(collection_draft())
    await documents.persist_batch(
        collection.collection_id,
        [BatchItem(draft=draft("https://arxiv.org/abs/1"), relevance_rank=1)],
        {},
    )
    assert await documents.purge_orphans(datetime.now(UTC) - timedelta(days=90), 1000) == 0
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM collection_documents")
        await conn.execute("UPDATE documents SET fetched_at = now() - interval '200 days'")
    assert await documents.purge_orphans(datetime.now(UTC) - timedelta(days=90), 1000) == 1
