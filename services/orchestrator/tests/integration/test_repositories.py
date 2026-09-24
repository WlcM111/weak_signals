"""Репозитории PostgreSQL orchestrator: миграция, очередь, аренды, транзакции снимка.

Запуск: `uv run pytest services/orchestrator/tests/integration -m integration` (нужен Docker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.adapters.outbound.postgres.idempotency_repository import (
    PostgresIdempotencyRepository,
)
from orchestrator.adapters.outbound.postgres.job_repository import PostgresJobRepository
from orchestrator.adapters.outbound.postgres.result_repository import PostgresResultRepository
from orchestrator.domain.entities import (
    ExcludedCandidate,
    FeatureRow,
    Job,
    Query,
    ResultItem,
    SourceRow,
)
from orchestrator.domain.values import (
    Decision,
    FeatureDirection,
    JobStats,
    JobStatus,
    NarrativeStatus,
    SummaryKind,
    TrustLevel,
)
from ws_common.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def make_query() -> Query:
    """Запрос пользователя."""
    return Query.create(str(uuid7()), "слабые сигналы в ИИ", 15)


def make_item(job_id: str, rank: int, candidate_id: str) -> ResultItem:
    """Элемент выдачи с признаками и источниками."""
    return ResultItem(
        job_id=job_id,
        rank=rank,
        candidate_id=candidate_id,
        title_ru=f"Технология {rank}",
        title_auto=f"Кандидат {rank}",
        score=0.9,
        decision_reason="MODEL_SCORE",
        decision_explanation_ru="Объяснение решения",
        description_ru="Описание",
        advantage_ru="Преимущество",
        case_example_ru="Кейс",
        explanation_ru="Почему слабый сигнал",
        narrative_status=NarrativeStatus.GENERATED,
        llm_provider="gigachat",
        llm_model="GigaChat-2-Pro",
        prompt_version="insight_v1",
        document_count=5,
        features=(
            FeatureRow("lex_emergence_score", "Лексика ранней стадии", 0.8, 0.4,
                       FeatureDirection.SUPPORTS_WEAK_SIGNAL, 1),
        ),
        sources=(
            SourceRow(
                position=1,
                document_id=str(uuid7()),
                title="Источник",
                url="https://example.org/1",
                source_type="SCIENTIFIC_PUBLICATION",
                source_key="openalex",
                language_code="en",
                trust_level=TrustLevel.HIGH,
                summary_ru="Резюме на русском",
                summary_kind=SummaryKind.GENERATIVE_SUMMARY,
                snippet="Сниппет",
                similarity=0.88,
                published_at=NOW,
            ),
        ),
    )


async def submit(pool) -> tuple[PostgresJobRepository, Job]:  # noqa: ANN001
    """Создаёт задание через репозиторий."""
    jobs = PostgresJobRepository(pool)
    query = make_query()
    job = Job(job_id=str(uuid7()), query_id=query.query_id)
    # Срок жизни ключа отсчитывается от реального времени: фиксированная дата NOW со временем
    # становится прошлым, и репозиторий перестаёт находить ключ (фильтр expires_at > now()).
    stored = await jobs.insert(
        query, job, f"key-{job.job_id[:8]}:submit", "hash", datetime.now(UTC) + timedelta(hours=24)
    )
    return jobs, stored


async def test_migration_creates_schema(pool) -> None:  # noqa: ANN001
    """Миграция создаёт десять таблиц схемы."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) AS total FROM information_schema.tables WHERE table_schema='orchestrator'"
        )
        row = await cur.fetchone()
    assert row["total"] == 10


async def test_insert_is_transactional(pool) -> None:  # noqa: ANN001
    """Запрос, задание, ключ и событие создаются вместе."""
    jobs, job = await submit(pool)
    assert job.status is JobStatus.QUEUED
    idempotency = PostgresIdempotencyRepository(pool)
    stored = await idempotency.get(f"key-{job.job_id[:8]}:submit")
    assert stored is not None
    assert stored[0] == job.job_id


async def test_claim_is_exclusive(pool) -> None:  # noqa: ANN001
    """Одно задание достаётся только одному воркеру."""
    jobs, job = await submit(pool)
    first = await jobs.claim_next("worker-1", 60, NOW)
    second = await jobs.claim_next("worker-2", 60, NOW)
    assert first is not None and first.job_id == job.job_id
    assert second is None


async def test_heartbeat_only_for_owner(pool) -> None:  # noqa: ANN001
    """Heartbeat продлевает аренду только владельцу."""
    jobs, job = await submit(pool)
    claimed = await jobs.claim_next("worker-1", 60, NOW)
    alive, _ = await jobs.heartbeat(claimed.job_id, "worker-1", 60)
    assert alive
    alien, _ = await jobs.heartbeat(claimed.job_id, "worker-2", 60)
    assert not alien


async def test_expired_lease_is_released(pool) -> None:  # noqa: ANN001
    """Задание с истёкшей арендой возвращается в очередь с увеличением попытки."""
    jobs, job = await submit(pool)
    claimed = await jobs.claim_next("worker-1", 60, NOW)
    await jobs.transition(claimed, JobStatus.COLLECTING, "worker-1")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE job_id = %s",
            (claimed.job_id,),
        )
    released = await jobs.release_expired_leases(datetime.now(UTC))
    assert released == 1
    refreshed = await jobs.get(claimed.job_id)
    assert refreshed.status is JobStatus.QUEUED
    assert refreshed.attempt == 1


async def test_result_snapshot_is_transactional(pool) -> None:  # noqa: ANN001
    """Элемент, его признаки и источники пишутся одной транзакцией и не дублируются."""
    jobs, job = await submit(pool)
    results = PostgresResultRepository(pool)
    candidate_id = str(uuid7())
    first = await results.add_item(make_item(job.job_id, 1, candidate_id))
    second = await results.add_item(make_item(job.job_id, 1, candidate_id))
    assert first == second
    items, excluded, stats = await results.get_results(job.job_id)
    assert len(items) == 1
    assert len(items[0].features) == 1
    assert len(items[0].sources) == 1


async def test_excluded_and_stats(pool) -> None:  # noqa: ANN001
    """Исключённые кандидаты и статистика сохраняются и читаются."""
    jobs, job = await submit(pool)
    results = PostgresResultRepository(pool)
    await results.add_excluded(
        job.job_id,
        [
            ExcludedCandidate(
                candidate_id=str(uuid7()),
                title_auto="Зрелая технология",
                score=0.2,
                decision=Decision.MATURE,
                decision_reason="MATURITY_LEXICON",
                decision_explanation_ru="Рынок сформирован",
                document_count=7,
            )
        ],
    )
    await results.set_stats(job.job_id, JobStats(documents_collected=120, weak_signals_total=15))
    items, excluded, stats = await results.get_results(job.job_id)
    assert len(excluded) == 1
    assert stats.documents_collected == 120


async def test_idempotency_purge(pool) -> None:  # noqa: ANN001
    """Истёкшие ключи удаляются фоновой задачей."""
    jobs, job = await submit(pool)
    idempotency = PostgresIdempotencyRepository(pool)
    purged = await idempotency.purge_expired(datetime.now(UTC) + timedelta(days=2))
    assert purged >= 1
