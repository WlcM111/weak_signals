"""Репозитории insight: инсайты, расширения запросов, журнал вызовов, реестр промптов."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from insight.application.dto import CallRecord, PromptDefinition
from insight.domain.entities import (
    GroundingCheck,
    Insight,
    Narrative,
    QueryExpansion,
    SourceSummary,
)
from insight.domain.values import InsightStatus, Provenance, SummaryKind

_INSIGHT_COLUMNS = (
    "insight_id, idempotency_key, input_hash, candidate_id, prompt_version, status, provider, "
    "model, title_ru, description_ru, advantage_ru, case_example_ru, case_document_id, "
    "explanation_ru, grounding_passed, unsupported_numbers, unknown_document_refs, "
    "features_mentioned, attempts, created_at"
)
_INSERT_INSIGHT = f"""
INSERT INTO insights (
  idempotency_key, input_hash, candidate_id, prompt_version, status, provider, model,
  title_ru, description_ru, advantage_ru, case_example_ru, case_document_id, explanation_ru,
  grounding_passed, unsupported_numbers, unknown_document_refs, features_mentioned, attempts
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING {_INSIGHT_COLUMNS}
"""
_INSERT_SOURCE = """
INSERT INTO insight_sources (insight_id, document_id, position, summary_ru, summary_kind)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (insight_id, document_id) DO NOTHING
"""
_SELECT_BY_KEY = f"SELECT {_INSIGHT_COLUMNS} FROM insights WHERE idempotency_key = %s"
_SELECT_BY_HASH = (
    f"SELECT {_INSIGHT_COLUMNS} FROM insights WHERE input_hash = %s AND created_at >= %s "
    "ORDER BY created_at DESC LIMIT 1"
)
_SELECT_SOURCES = (
    "SELECT document_id, position, summary_ru, summary_kind FROM insight_sources "
    "WHERE insight_id = %s ORDER BY position"
)
_SELECT_EXPANSION = (
    "SELECT query_norm, ru_terms, en_terms, domain_tags, used_fallback, provider, model, "
    "prompt_version FROM query_expansions WHERE query_norm = %s AND prompt_version = %s AND NOT used_fallback"
)
_UPSERT_EXPANSION = """
INSERT INTO query_expansions
  (query_norm, prompt_version, ru_terms, en_terms, domain_tags, used_fallback, provider, model)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (query_norm) DO UPDATE SET
  prompt_version = EXCLUDED.prompt_version,
  ru_terms = EXCLUDED.ru_terms,
  en_terms = EXCLUDED.en_terms,
  domain_tags = EXCLUDED.domain_tags,
  used_fallback = EXCLUDED.used_fallback,
  provider = EXCLUDED.provider,
  model = EXCLUDED.model,
  created_at = now()
"""
_INSERT_CALL = """
INSERT INTO llm_calls
  (purpose, idempotency_key, provider, model, prompt_version, request_sha256, prompt_tokens,
   completion_tokens, latency_ms, status, error_code)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
_TOKENS_TODAY = (
    "SELECT COALESCE(sum(COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0)), 0) AS total "
    "FROM llm_calls WHERE created_at >= date_trunc('day', %s::timestamptz)"
)
_UPSERT_PROMPT = """
INSERT INTO prompt_versions
  (prompt_version, purpose, template_sha256, output_schema_version, description)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (prompt_version) DO UPDATE SET
  template_sha256 = EXCLUDED.template_sha256,
  output_schema_version = EXCLUDED.output_schema_version,
  description = EXCLUDED.description
"""


class PostgresInsightRepository:
    """Реализация порта `InsightRepository`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get_by_idempotency_key(self, key: str) -> Insight | None:
        """Инсайт по ключу идемпотентности."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_BY_KEY, (key,))
            row = await cur.fetchone()
            return await self._with_sources(cur, row)

    async def get_by_input_hash(self, input_hash: str, not_older_than: datetime) -> Insight | None:
        """Свежий инсайт с тем же входом (кеш по содержимому)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_BY_HASH, (input_hash, not_older_than))
            row = await cur.fetchone()
            return await self._with_sources(cur, row)

    async def save(self, insight: Insight) -> Insight:
        """Одной транзакцией пишет инсайт и резюме; повтор по ключу возвращает существующий."""
        narrative = insight.narrative
        grounding = insight.grounding
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _INSERT_INSIGHT,
                (
                    insight.idempotency_key,
                    insight.input_hash,
                    insight.candidate_id,
                    insight.prompt_version,
                    insight.status.value,
                    insight.provenance.provider,
                    insight.provenance.model,
                    narrative.title_ru,
                    narrative.description_ru,
                    narrative.advantage_ru,
                    narrative.case_example_ru,
                    narrative.case_document_id or None,
                    narrative.explanation_ru,
                    grounding.passed,
                    grounding.unsupported_numbers,
                    grounding.unknown_document_refs,
                    grounding.features_mentioned,
                    insight.attempts,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(_SELECT_BY_KEY, (insight.idempotency_key,))
                existing = await cur.fetchone()
                return await self._with_sources(cur, existing) or insight
            insight_id = str(row["insight_id"])
            for summary in insight.summaries:
                await cur.execute(
                    _INSERT_SOURCE,
                    (
                        insight_id,
                        summary.document_id,
                        summary.position,
                        summary.summary_ru,
                        summary.kind.value,
                    ),
                )
        return _replace_identity(insight, insight_id, row["created_at"])

    async def _with_sources(self, cur: Any, row: dict | None) -> Insight | None:
        """Достраивает инсайт резюме источников."""
        if row is None:
            return None
        await cur.execute(_SELECT_SOURCES, (row["insight_id"],))
        summaries = [dict(item) for item in await cur.fetchall()]
        return _row_to_insight(dict(row), summaries)


class PostgresExpansionRepository:
    """Реализация порта `ExpansionRepository`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get(self, query_norm: str, prompt_version: str) -> QueryExpansion | None:
        """Сохранённое удачное расширение этой версии промпта; расширение другой версии или резервное не берётся."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_EXPANSION, (query_norm, prompt_version))
            row = await cur.fetchone()
        if row is None:
            return None
        return QueryExpansion(
            query_norm=row["query_norm"],
            ru_terms=tuple(row["ru_terms"]),
            en_terms=tuple(row["en_terms"]),
            domain_tags=tuple(row["domain_tags"] or ()),
            used_fallback=row["used_fallback"],
            provenance=Provenance(
                provider=row["provider"], model=row["model"] or "", prompt_version=row["prompt_version"]
            ),
        )

    async def save(self, expansion: QueryExpansion, prompt_version: str) -> None:
        """Сохраняет расширение запроса."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _UPSERT_EXPANSION,
                (
                    expansion.query_norm,
                    prompt_version,
                    list(expansion.ru_terms),
                    list(expansion.en_terms),
                    list(expansion.domain_tags),
                    expansion.used_fallback,
                    expansion.provenance.provider,
                    expansion.provenance.model,
                ),
            )


class PostgresLLMCallLog:
    """Реализация порта `LLMCallLog`: только метаданные вызовов."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(self, entry: CallRecord) -> None:
        """Пишет запись журнала."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _INSERT_CALL,
                (
                    entry.purpose.value,
                    entry.idempotency_key,
                    entry.provider,
                    entry.model,
                    entry.prompt_version,
                    entry.request_sha256,
                    entry.prompt_tokens,
                    entry.completion_tokens,
                    entry.latency_ms,
                    entry.status.value,
                    entry.error_code,
                ),
            )

    async def tokens_used_today(self, now: datetime) -> int:
        """Сумма токенов за текущие сутки."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_TOKENS_TODAY, (now,))
            row = await cur.fetchone()
        return int(row["total"]) if row else 0


class PostgresPromptRegistry:
    """Реализация порта `PromptRegistry`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def register(self, definitions: Sequence[PromptDefinition]) -> None:
        """Регистрирует версии промптов при старте."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for definition in definitions:
                await cur.execute(
                    _UPSERT_PROMPT,
                    (
                        definition.prompt_version,
                        definition.purpose.value,
                        definition.template_sha256,
                        definition.output_schema_version,
                        definition.description,
                    ),
                )


def _row_to_insight(row: dict[str, Any], summaries: Sequence[dict[str, Any]]) -> Insight:
    """Строки таблиц → сущность инсайта."""
    return Insight(
        insight_id=str(row["insight_id"]),
        idempotency_key=row["idempotency_key"],
        input_hash=row["input_hash"],
        candidate_id=str(row["candidate_id"]),
        prompt_version=row["prompt_version"],
        status=InsightStatus(row["status"]),
        narrative=Narrative(
            title_ru=row["title_ru"],
            description_ru=row["description_ru"],
            advantage_ru=row["advantage_ru"],
            case_example_ru=row["case_example_ru"],
            explanation_ru=row["explanation_ru"],
            case_document_id=str(row["case_document_id"]) if row["case_document_id"] else "",
        ),
        grounding=GroundingCheck(
            passed=row["grounding_passed"],
            unsupported_numbers=row["unsupported_numbers"],
            unknown_document_refs=row["unknown_document_refs"],
            features_mentioned=row["features_mentioned"],
        ),
        provenance=Provenance(
            provider=row["provider"], model=row["model"] or "", prompt_version=row["prompt_version"]
        ),
        summaries=tuple(
            SourceSummary(
                document_id=str(item["document_id"]),
                summary_ru=item["summary_ru"],
                kind=SummaryKind(item["summary_kind"]),
                position=item["position"],
            )
            for item in summaries
        ),
        attempts=row["attempts"],
        created_at=row["created_at"],
    )


def _replace_identity(insight: Insight, insight_id: str, created_at: datetime) -> Insight:
    """Возвращает копию инсайта с присвоенным идентификатором и временем создания."""
    return Insight(
        idempotency_key=insight.idempotency_key,
        input_hash=insight.input_hash,
        candidate_id=insight.candidate_id,
        prompt_version=insight.prompt_version,
        status=insight.status,
        narrative=insight.narrative,
        grounding=insight.grounding,
        provenance=insight.provenance,
        summaries=insight.summaries,
        insight_id=insight_id,
        attempts=insight.attempts,
        from_cache=False,
        created_at=created_at,
    )
