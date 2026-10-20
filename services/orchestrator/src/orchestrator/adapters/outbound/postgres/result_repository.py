"""Репозиторий снимка результата: элементы, признаки, источники, исключённые, статистика."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Json
from ws_common.jsonsafe import json_safe

from orchestrator.adapters.outbound.postgres.mappers import (
    ITEM_COLUMNS,
    STATS_COLUMNS,
    row_to_excluded,
    row_to_item,
    row_to_stats,
)
from orchestrator.application.dto import TraceView
from orchestrator.domain.entities import ExcludedCandidate, ResultItem
from orchestrator.domain.values import JobProgress, JobStats, JobStatus

_INSERT_ITEM = """
INSERT INTO result_items (
  job_id, rank, candidate_id, title_ru, title_auto, score, decision_reason,
  decision_explanation_ru, description_ru, advantage_ru, case_example_ru, case_document_id,
  explanation_ru, narrative_status, llm_provider, llm_model, prompt_version, predicted_stage,
  predicted_trend, document_count
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (job_id, candidate_id) DO NOTHING
RETURNING item_id
"""
_SELECT_ITEM_ID = "SELECT item_id FROM result_items WHERE job_id = %s AND candidate_id = %s"
_INSERT_FEATURE = """
INSERT INTO result_item_features
  (item_id, feature_name, label_ru, value, contribution, direction, display_order)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (item_id, feature_name) DO NOTHING
"""
_INSERT_SOURCE = """
INSERT INTO result_item_sources
  (item_id, position, document_id, title, url, published_at, source_type, source_key,
   language_code, trust_level, summary_ru, summary_kind, snippet, similarity)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (item_id, position) DO NOTHING
"""
_INSERT_EXCLUDED = """
INSERT INTO excluded_candidates
  (job_id, candidate_id, title_auto, score, decision, decision_reason,
   decision_explanation_ru, document_count)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (job_id, candidate_id) DO NOTHING
"""
_UPSERT_STATS = f"""
INSERT INTO job_stats (job_id, {STATS_COLUMNS})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (job_id) DO UPDATE SET
  http_requests_total = EXCLUDED.http_requests_total,
  sources_processed = EXCLUDED.sources_processed,
  documents_collected = EXCLUDED.documents_collected,
  candidates_found = EXCLUDED.candidates_found,
  weak_signals_total = EXCLUDED.weak_signals_total,
  weak_signals_confident = EXCLUDED.weak_signals_confident,
  collect_ms = EXCLUDED.collect_ms,
  analyze_ms = EXCLUDED.analyze_ms,
  narrate_ms = EXCLUDED.narrate_ms,
  narratives_generated = EXCLUDED.narratives_generated,
  narratives_fallback = EXCLUDED.narratives_fallback,
  model_version_id = EXCLUDED.model_version_id,
  expand_used_fallback = EXCLUDED.expand_used_fallback
"""
_SELECT_STATS = f"SELECT {STATS_COLUMNS} FROM job_stats WHERE job_id = %s"
_SELECT_ITEMS = f"SELECT {ITEM_COLUMNS} FROM result_items WHERE job_id = %s ORDER BY rank"
_SELECT_ITEM = f"SELECT {ITEM_COLUMNS} FROM result_items WHERE item_id = %s"
_SELECT_FEATURES = (
    "SELECT item_id, feature_name, label_ru, value, contribution, direction, display_order "
    "FROM result_item_features WHERE item_id = ANY(%s::uuid[])"
)
_SELECT_SOURCES = (
    "SELECT item_id, position, document_id, title, url, published_at, source_type, source_key, "
    "language_code, trust_level, summary_ru, summary_kind, snippet, similarity "
    "FROM result_item_sources WHERE item_id = ANY(%s::uuid[])"
)
_SELECT_EXCLUDED = (
    "SELECT candidate_id, title_auto, score, decision, decision_reason, decision_explanation_ru, "
    "document_count FROM excluded_candidates WHERE job_id = %s ORDER BY score DESC"
)
_COUNT_ITEMS = "SELECT count(*) AS total FROM result_items WHERE job_id = %s"


class PostgresResultRepository:
    """Реализация порта `ResultRepository`."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def add_item(self, item: ResultItem) -> str:
        """Одной транзакцией пишет элемент, его признаки и источники."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _INSERT_ITEM,
                (
                    item.job_id,
                    item.rank,
                    item.candidate_id,
                    item.title_ru,
                    item.title_auto,
                    item.score,
                    item.decision_reason,
                    item.decision_explanation_ru,
                    item.description_ru,
                    item.advantage_ru,
                    item.case_example_ru,
                    item.case_document_id or None,
                    item.explanation_ru,
                    item.narrative_status.value,
                    item.llm_provider,
                    item.llm_model,
                    item.prompt_version,
                    item.predicted_stage,
                    item.predicted_trend,
                    item.document_count,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(_SELECT_ITEM_ID, (item.job_id, item.candidate_id))
                row = await cur.fetchone()
                return str(row["item_id"]) if row else ""
            item_id = str(row["item_id"])
            for feature in item.features:
                await cur.execute(
                    _INSERT_FEATURE,
                    (
                        item_id,
                        feature.feature_name,
                        feature.label_ru,
                        feature.value,
                        feature.contribution,
                        feature.direction.value,
                        feature.display_order,
                    ),
                )
            for source in item.sources:
                await cur.execute(
                    _INSERT_SOURCE,
                    (
                        item_id,
                        source.position,
                        source.document_id,
                        source.title,
                        source.url,
                        source.published_at,
                        source.source_type,
                        source.source_key,
                        source.language_code,
                        source.trust_level.value,
                        source.summary_ru,
                        source.summary_kind.value,
                        source.snippet,
                        source.similarity,
                    ),
                )
        return item_id

    async def add_excluded(self, job_id: str, batch: Sequence[ExcludedCandidate]) -> int:
        """Пишет исключённых кандидатов пачкой."""
        if not batch:
            return 0
        payload = [
            (
                job_id,
                candidate.candidate_id,
                candidate.title_auto,
                candidate.score,
                candidate.decision.value,
                candidate.decision_reason,
                candidate.decision_explanation_ru,
                candidate.document_count,
            )
            for candidate in batch
        ]
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(_INSERT_EXCLUDED, payload)
        return len(payload)

    async def set_stats(self, job_id: str, stats: JobStats) -> None:
        """Сохраняет статистику задания."""
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                _UPSERT_STATS,
                (
                    job_id,
                    stats.http_requests_total,
                    stats.sources_processed,
                    stats.documents_collected,
                    stats.candidates_found,
                    stats.weak_signals_total,
                    stats.weak_signals_confident,
                    stats.collect_ms,
                    stats.analyze_ms,
                    stats.narrate_ms,
                    stats.narratives_generated,
                    stats.narratives_fallback,
                    stats.model_version_id or None,
                    stats.expand_used_fallback,
                ),
            )

    async def get_stats(self, job_id: str) -> JobStats | None:
        """Статистика задания."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_STATS, (job_id,))
            row = await cur.fetchone()
        return row_to_stats(dict(row)) if row else None

    async def count_items(self, job_id: str) -> int:
        """Число записанных элементов."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_COUNT_ITEMS, (job_id,))
            row = await cur.fetchone()
        return int(row["total"]) if row else 0

    async def get_results(
        self, job_id: str
    ) -> tuple[list[ResultItem], list[ExcludedCandidate], JobStats]:
        """Снимок результата задания целиком."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_ITEMS, (job_id,))
            rows = [dict(row) for row in await cur.fetchall()]
            identifiers = [row["item_id"] for row in rows]
            features: dict[str, list[dict[str, Any]]] = {}
            sources: dict[str, list[dict[str, Any]]] = {}
            if identifiers:
                await cur.execute(_SELECT_FEATURES, (identifiers,))
                for item in await cur.fetchall():
                    features.setdefault(str(item["item_id"]), []).append(dict(item))
                await cur.execute(_SELECT_SOURCES, (identifiers,))
                for item in await cur.fetchall():
                    sources.setdefault(str(item["item_id"]), []).append(dict(item))
            await cur.execute(_SELECT_EXCLUDED, (job_id,))
            excluded = [row_to_excluded(dict(row)) for row in await cur.fetchall()]
            await cur.execute(_SELECT_STATS, (job_id,))
            stats_row = await cur.fetchone()
        items = [
            row_to_item(
                row, features.get(str(row["item_id"]), []), sources.get(str(row["item_id"]), [])
            )
            for row in rows
        ]
        return items, excluded, row_to_stats(dict(stats_row) if stats_row else None)

    async def get_item(self, item_id: str) -> ResultItem | None:
        """Элемент выдачи по идентификатору."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_ITEM, (item_id,))
            row = await cur.fetchone()
            if row is None:
                return None
            await cur.execute(_SELECT_FEATURES, ([item_id],))
            features = [dict(item) for item in await cur.fetchall()]
            await cur.execute(_SELECT_SOURCES, ([item_id],))
            sources = [dict(item) for item in await cur.fetchall()]
        return row_to_item(dict(row), features, sources)

    async def progress(self, job_id: str) -> JobProgress | None:
        """Прогресс по записанным данным: статистика стадий и число готовых элементов."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT j.status, q.requested_top_n FROM jobs j "
                "LEFT JOIN queries q ON q.query_id = j.query_id WHERE j.job_id = %s",
                (job_id,),
            )
            job_row = await cur.fetchone()
            if job_row is None:
                return None
            await cur.execute(_SELECT_STATS, (job_id,))
            stats_row = await cur.fetchone()
            await cur.execute(_COUNT_ITEMS, (job_id,))
            items_row = await cur.fetchone()
        stats = row_to_stats(dict(stats_row) if stats_row else None)
        stage = JobStatus(job_row["status"])
        requested_top_n = int(job_row.get("requested_top_n") or 0)
        items_done = int(items_row["total"]) if items_row else 0
        stats_done = stats.narratives_generated + stats.narratives_fallback
        if stage.is_terminal and items_done > 0:
            narratives_done = items_done
            narratives_total = items_done
        else:
            narratives_total = (
                min(stats.weak_signals_total, requested_top_n)
                if requested_top_n > 0 and stats.weak_signals_total > 0
                else stats.weak_signals_total
            )
            raw_done = max(items_done, stats_done)
            narratives_done = min(raw_done, narratives_total) if narratives_total > 0 else raw_done
        return JobProgress(
            stage=stage,
            http_requests_total=stats.http_requests_total,
            sources_processed=stats.sources_processed,
            documents_collected=stats.documents_collected,
            candidates_found=stats.candidates_found,
            narratives_done=narratives_done,
            narratives_total=narratives_total,
        )

    async def save_trace(
        self, job_id: str, stage: str, payload: dict[str, object], keep_previous: bool = False
    ) -> None:
        """Трасса стадии задания: одна строка на стадию, повтор стадии перезаписывает её.

        keep_previous: прежняя строка стадии в той же инструкции копируется в `stage@N` (N — номер прошлого
        прогона, 1 — первый; updated_at сохраняется) — без смены схемы, stage ≤ 32 символов (0003_job_traces.sql).
        """
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            if keep_previous:
                await cur.execute(_UPSERT_TRACE_KEEP, (job_id, stage, job_id, stage, Json(json_safe(payload))))
            else:
                await cur.execute(_UPSERT_TRACE, (job_id, stage, Json(json_safe(payload))))

    async def get_trace(self, job_id: str, stage: str) -> TraceView | None:
        """Трасса стадии (или прошлого прогона `stage@N`) и список записанных стадий задания."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SELECT_TRACE, (job_id, job_id, stage))
            row = await cur.fetchone()
        if row is None:
            return None
        return TraceView(job_id=job_id, stage=stage, payload=row["payload"], updated_at=row["updated_at"],
                         stages=tuple(row["stages"]))


_UPSERT_TRACE = """
INSERT INTO job_traces (job_id, stage, payload)
VALUES (%s, %s, %s)
ON CONFLICT (job_id, stage) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
"""
_UPSERT_TRACE_KEEP = """
WITH previous AS (
  INSERT INTO job_traces (job_id, stage, payload, updated_at)
  SELECT t.job_id, t.stage || '@' || (SELECT count(*) + 1 FROM job_traces a
                                      WHERE a.job_id = t.job_id AND starts_with(a.stage, t.stage || '@')),
         t.payload, t.updated_at
  FROM job_traces t WHERE t.job_id = %s AND t.stage = %s
  ON CONFLICT (job_id, stage) DO NOTHING
)
INSERT INTO job_traces (job_id, stage, payload)
VALUES (%s, %s, %s)
ON CONFLICT (job_id, stage) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
"""
_SELECT_TRACE = """
SELECT payload, updated_at,
       ARRAY(SELECT s.stage FROM job_traces s WHERE s.job_id = %s ORDER BY s.stage) AS stages
FROM job_traces WHERE job_id = %s AND stage = %s
"""
