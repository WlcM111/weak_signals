"""Репозиторий кандидатов: транзакционная запись результата анализа и постраничное чтение."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from analyzer.adapters.outbound.postgres.mappers import CANDIDATE_COLUMNS
from analyzer.domain.entities import Candidate, ClusterDocument, FeatureContribution
from analyzer.domain.rules import direction_of
from analyzer.domain.values import (
    AnalysisStats,
    Decision,
    DecisionReason,
    FeatureDirection,
    SourceType,
    TrustLevel,
)

_DELETE_PREVIOUS = "DELETE FROM candidates WHERE analysis_id = %s"

_INSERT_CANDIDATE = """
INSERT INTO candidates (
  analysis_id, cluster_index, rank, title_auto, keyphrases, score, decision, decision_reason,
  decision_explanation_ru, document_count, query_relevance, predicted_stage, predicted_trend
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING candidate_id
"""

_INSERT_FEATURE = """
INSERT INTO candidate_features (candidate_id, feature_name, value, contribution)
VALUES (%s, %s, %s, %s)
"""

_INSERT_DOCUMENT = """
INSERT INTO candidate_documents (
  candidate_id, document_id, similarity, is_evidence, snippet, source_type, trust_level, published_year
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_COMPLETE_ANALYSIS = """
UPDATE analyses
SET status = 'COMPLETED', finished_at = %s, lease_owner = NULL, lease_expires_at = NULL,
    error_code = NULL, error_message = NULL,
    documents_input = %s, documents_after_dedup = %s, clusters_total = %s, candidates_scored = %s,
    weak_signals_total = %s, weak_signals_confident = %s, excluded_mature = %s,
    excluded_hype_or_noise = %s, excluded_insufficient_evidence = %s, excluded_off_topic = %s,
    duration_ms = %s
WHERE analysis_id = %s AND lease_owner = %s AND status = 'RUNNING'
RETURNING analysis_id
"""

_ORDERED_CTE = f"""
WITH ordered AS (
  SELECT {CANDIDATE_COLUMNS},
         (CASE WHEN decision = 'WEAK_SIGNAL' THEN 0 ELSE 1 END) AS grp,
         (CASE WHEN decision = 'WEAK_SIGNAL' THEN rank::double precision ELSE -score END) AS ord
  FROM candidates
  WHERE analysis_id = %(analysis_id)s AND (%(include_excluded)s OR decision = 'WEAK_SIGNAL')
)
SELECT * FROM ordered
WHERE (%(after)s::boolean IS FALSE)
   OR ((grp, ord, cluster_index) > (%(grp)s, %(ord)s, %(cluster_index)s))
ORDER BY grp, ord, cluster_index
LIMIT %(limit)s
"""

_COUNT = """
SELECT count(*) AS total FROM candidates
WHERE analysis_id = %(analysis_id)s AND (%(include_excluded)s OR decision = 'WEAK_SIGNAL')
"""

_SELECT_FEATURES = (
    "SELECT candidate_id, feature_name, value, contribution FROM candidate_features "
    "WHERE candidate_id = ANY(%s::uuid[])"
)

_SELECT_DOCUMENTS = (
    "SELECT candidate_id, document_id, similarity, is_evidence, snippet, source_type, trust_level, "
    "published_year FROM candidate_documents WHERE candidate_id = ANY(%s::uuid[]) "
    "ORDER BY similarity DESC"
)


class _LeaseLostDuringSave(Exception):
    """Внутренний сигнал отката транзакции при потере аренды."""


class PostgresCandidateRepository:
    """Реализация порта `CandidateRepository`."""

    def __init__(self, pool: Any, feature_labels: dict[str, str] | None = None) -> None:
        self._pool = pool
        self._feature_labels = feature_labels or {}

    def save_results(
        self,
        analysis_id: str,
        owner: str,
        candidates: Sequence[Candidate],
        stats: AnalysisStats,
        finished_at: datetime,
    ) -> bool:
        """Одна транзакция: кандидаты прошлой попытки удаляются, новые записываются, анализ завершается."""
        try:
            with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(_DELETE_PREVIOUS, (analysis_id,))
                for candidate in candidates:
                    cur.execute(
                        _INSERT_CANDIDATE,
                        (
                            analysis_id,
                            candidate.cluster_index,
                            candidate.rank,
                            candidate.title_auto,
                            list(candidate.keyphrases),
                            candidate.score,
                            candidate.decision.value,
                            candidate.decision_reason.value,
                            candidate.decision_explanation_ru,
                            candidate.document_count,
                            candidate.query_relevance,
                            candidate.predicted_stage,
                            candidate.predicted_trend,
                        ),
                    )
                    row = cur.fetchone()
                    candidate_id = str(row["candidate_id"])
                    cur.executemany(
                        _INSERT_FEATURE,
                        [
                            (candidate_id, item.feature_name, item.value, item.contribution)
                            for item in candidate.features
                        ],
                    )
                    cur.executemany(
                        _INSERT_DOCUMENT,
                        [
                            (
                                candidate_id,
                                document.document_id,
                                document.similarity,
                                document.is_evidence,
                                document.snippet or None,
                                document.source_type.value,
                                document.trust_level.value,
                                document.published_year,
                            )
                            for document in candidate.documents
                        ],
                    )
                cur.execute(
                    _COMPLETE_ANALYSIS,
                    (
                        finished_at,
                        stats.documents_input,
                        stats.documents_after_dedup,
                        stats.clusters_total,
                        stats.candidates_scored,
                        stats.weak_signals_total,
                        stats.weak_signals_confident,
                        stats.excluded_mature,
                        stats.excluded_hype_or_noise,
                        stats.excluded_insufficient_evidence,
                        stats.excluded_off_topic,
                        stats.duration_ms,
                        analysis_id,
                        owner,
                    ),
                )
                if cur.fetchone() is None:
                    raise _LeaseLostDuringSave()
        except _LeaseLostDuringSave:
            return False
        return True

    def list_candidates(
        self,
        analysis_id: str,
        *,
        include_excluded: bool,
        after: tuple[int, float, int] | None,
        limit: int,
    ) -> tuple[list[Candidate], int]:
        """Страница кандидатов со стабильным порядком и общее число строк."""
        params = {
            "analysis_id": analysis_id,
            "include_excluded": include_excluded,
            "after": after is not None,
            "grp": after[0] if after else 0,
            "ord": after[1] if after else 0.0,
            "cluster_index": after[2] if after else 0,
            "limit": limit,
        }
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_ORDERED_CTE, params)
            rows = [dict(row) for row in cur.fetchall()]
            cur.execute(_COUNT, params)
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            ids = [row["candidate_id"] for row in rows]
            features: dict[str, list] = {}
            documents: dict[str, list] = {}
            if ids:
                cur.execute(_SELECT_FEATURES, (ids,))
                for item in cur.fetchall():
                    features.setdefault(str(item["candidate_id"]), []).append(dict(item))
                cur.execute(_SELECT_DOCUMENTS, (ids,))
                for item in cur.fetchall():
                    documents.setdefault(str(item["candidate_id"]), []).append(dict(item))
        built = [
            self._build_candidate(
                row,
                features.get(str(row["candidate_id"]), []),
                documents.get(str(row["candidate_id"]), []),
            )
            for row in rows
        ]
        return built, total

    def _build_candidate(
        self, row: dict[str, Any], feature_rows: list[dict[str, Any]], document_rows: list[dict[str, Any]]
    ) -> Candidate:
        """Собирает кандидата из строк трёх таблиц."""
        features = tuple(
            sorted(
                (
                    FeatureContribution(
                        feature_name=item["feature_name"],
                        value=float(item["value"]),
                        contribution=float(item["contribution"]),
                        label_ru=self._feature_labels.get(item["feature_name"], item["feature_name"]),
                        direction=(
                            direction_of(float(item["contribution"]))
                            if item["contribution"]
                            else FeatureDirection.NEUTRAL
                        ),
                    )
                    for item in feature_rows
                ),
                key=lambda item: (-abs(item.contribution), item.feature_name),
            )
        )
        documents = tuple(
            ClusterDocument(
                document_id=str(item["document_id"]),
                similarity=float(item["similarity"]),
                is_evidence=bool(item["is_evidence"]),
                snippet=item["snippet"] or "",
                source_type=SourceType(item["source_type"]),
                trust_level=TrustLevel(item["trust_level"]),
                published_year=item["published_year"],
            )
            for item in document_rows
        )
        source_counts: dict[SourceType, int] = {}
        year_counts: dict[int, int] = {}
        for document in documents:
            source_counts[document.source_type] = source_counts.get(document.source_type, 0) + 1
            if document.published_year is not None:
                year_counts[document.published_year] = year_counts.get(document.published_year, 0) + 1
        return Candidate(
            cluster_index=row["cluster_index"],
            title_auto=row["title_auto"],
            keyphrases=tuple(row["keyphrases"]),
            score=float(row["score"]),
            decision=Decision(row["decision"]),
            decision_reason=DecisionReason(row["decision_reason"]),
            decision_explanation_ru=row["decision_explanation_ru"],
            features=features,
            documents=documents,
            document_count=row["document_count"],
            query_relevance=float(row["query_relevance"]),
            source_type_counts=source_counts,
            year_counts=year_counts,
            rank=row["rank"],
            candidate_id=str(row["candidate_id"]),
            predicted_stage=row["predicted_stage"],
            predicted_trend=row["predicted_trend"],
        )
