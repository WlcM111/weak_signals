"""Репозитории PostgreSQL insight: транзакции, идемпотентность, журнал без текстов.

Запуск: `uv run pytest services/insight/tests/integration -m integration` (нужен Docker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from insight.adapters.outbound.postgres.repositories import (
    PostgresExpansionRepository,
    PostgresInsightRepository,
    PostgresLLMCallLog,
    PostgresPromptRegistry,
)
from insight.application.dto import CallRecord, PromptDefinition
from insight.domain.entities import (
    GroundingCheck,
    Insight,
    Narrative,
    QueryExpansion,
    SourceSummary,
)
from insight.domain.values import (
    CallStatus,
    InsightStatus,
    Provenance,
    Purpose,
    SummaryKind,
)
from ws_common.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
PROMPTS = [
    PromptDefinition("insight_v1", Purpose.INSIGHT, "a" * 64, "insight_llm_output.schema.json@1"),
    PromptDefinition("expand_v1", Purpose.EXPAND, "b" * 64, "expand_llm_output.schema.json@1"),
]


def make_insight(key: str, digest: str = "hash-1") -> Insight:
    """Инсайт с двумя резюме источников."""
    documents = [str(uuid7()), str(uuid7())]
    return Insight(
        idempotency_key=key,
        input_hash=digest,
        candidate_id=str(uuid7()),
        prompt_version="insight_v1",
        status=InsightStatus.GENERATED,
        narrative=Narrative(
            title_ru="Нейроморфные чипы",
            description_ru="Описание технологии по источникам.",
            advantage_ru="Потенциальное преимущество технологии.",
            case_example_ru="Кейс-пример из источника.",
            explanation_ru="Объяснение статуса слабого сигнала.",
            case_document_id=documents[0],
        ),
        grounding=GroundingCheck(passed=True, features_mentioned=2),
        provenance=Provenance(provider="gigachat", model="GigaChat-2-Pro", prompt_version="insight_v1"),
        summaries=tuple(
            SourceSummary(
                document_id=document,
                summary_ru=f"Резюме источника {position}.",
                kind=SummaryKind.GENERATIVE_SUMMARY,
                position=position,
            )
            for position, document in enumerate(documents, start=1)
        ),
        attempts=1,
    )


async def seed_prompts(pool) -> None:  # noqa: ANN001
    """Регистрирует версии промптов (внешний ключ для insights)."""
    await PostgresPromptRegistry(pool).register(PROMPTS)


async def test_migration_creates_schema(pool) -> None:  # noqa: ANN001
    """Миграция создаёт шесть таблиц схемы."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) AS total FROM information_schema.tables WHERE table_schema='insight'"
        )
        row = await cur.fetchone()
    assert row["total"] == 6


async def test_save_is_transactional(pool) -> None:  # noqa: ANN001
    """Инсайт и резюме источников пишутся вместе."""
    await seed_prompts(pool)
    repository = PostgresInsightRepository(pool)
    saved = await repository.save(make_insight("job-1:cand-1:insight:insight_v1"))
    assert saved.insight_id
    loaded = await repository.get_by_idempotency_key("job-1:cand-1:insight:insight_v1")
    assert loaded is not None
    assert len(loaded.summaries) == 2
    assert loaded.narrative.title_ru == "Нейроморфные чипы"


async def test_idempotency_key_is_unique(pool) -> None:  # noqa: ANN001
    """Повтор с тем же ключом не создаёт вторую запись."""
    await seed_prompts(pool)
    repository = PostgresInsightRepository(pool)
    first = await repository.save(make_insight("job-2:cand-1:insight:insight_v1"))
    second = await repository.save(make_insight("job-2:cand-1:insight:insight_v1"))
    assert first.insight_id == second.insight_id
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) AS total FROM insights")
        row = await cur.fetchone()
    assert row["total"] == 1


async def test_cache_by_input_hash(pool) -> None:  # noqa: ANN001
    """Кеш по содержимому входа находит свежую запись и пропускает устаревшую."""
    await seed_prompts(pool)
    repository = PostgresInsightRepository(pool)
    await repository.save(make_insight("job-3:cand-1:insight:insight_v1", digest="hash-cache"))
    fresh = await repository.get_by_input_hash("hash-cache", datetime.now(UTC) - timedelta(days=7))
    stale = await repository.get_by_input_hash("hash-cache", datetime.now(UTC) + timedelta(days=1))
    assert fresh is not None
    assert stale is None


async def test_call_log_has_no_texts(pool) -> None:  # noqa: ANN001
    """В журнале вызовов нет ни промптов, ни ответов — только метаданные."""
    log = PostgresLLMCallLog(pool)
    await log.record(
        CallRecord(
            purpose=Purpose.INSIGHT,
            provider="gigachat",
            model="GigaChat-2-Pro",
            prompt_version="insight_v1",
            request_sha256="c" * 64,
            latency_ms=1200,
            status=CallStatus.OK,
            prompt_tokens=800,
            completion_tokens=400,
        )
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT * FROM llm_calls")
        row = await cur.fetchone()
    assert set(row) == {
        "call_id", "purpose", "idempotency_key", "provider", "model", "prompt_version",
        "request_sha256", "prompt_tokens", "completion_tokens", "latency_ms", "status",
        "error_code", "created_at",
    }
    assert await log.tokens_used_today(datetime.now(UTC)) == 1200


async def test_expansion_cache(pool) -> None:  # noqa: ANN001
    """Расширение запроса сохраняется и читается по нормализованному тексту."""
    await seed_prompts(pool)
    repository = PostgresExpansionRepository(pool)
    expansion = QueryExpansion(
        query_norm="слабые сигналы в ии",
        ru_terms=("слабые сигналы в ии",),
        en_terms=("weak signals in ai",),
        domain_tags=("ai_infrastructure",),
        used_fallback=False,
        provenance=Provenance(provider="gigachat", model="GigaChat-2", prompt_version="expand_v1"),
    )
    await repository.save(expansion, "expand_v1")
    loaded = await repository.get("слабые сигналы в ии")
    assert loaded is not None
    assert loaded.en_terms == ("weak signals in ai",)
    assert loaded.used_fallback is False
