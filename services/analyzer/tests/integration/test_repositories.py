"""Репозитории PostgreSQL analyzer: миграции, транзакции, аренды, кеш, одна активная модель.

Запуск: `uv run pytest services/analyzer/tests/integration -m integration` (нужен Docker).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from analyzer.adapters.outbound.postgres.analysis_repository import PostgresAnalysisRepository
from analyzer.adapters.outbound.postgres.candidate_repository import PostgresCandidateRepository
from analyzer.adapters.outbound.postgres.embedding_cache import PostgresEmbeddingCache
from analyzer.adapters.outbound.postgres.model_version_repository import PostgresModelVersionRepository
from analyzer.application.dto import AnalysisDraft, CachedEmbedding
from analyzer.domain.entities import (
    Candidate,
    ClusterDocument,
    FeatureContribution,
    ModelMetrics,
    ModelVersion,
)
from analyzer.domain.values import (
    AnalysisParams,
    AnalysisStats,
    Decision,
    DecisionReason,
    FeatureDirection,
    OperationStatus,
    SourceType,
    TrustLevel,
)

pytestmark = pytest.mark.integration

COLLECTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOCUMENT_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def make_version(version_id: str = "wsclf-2026.09.15-1") -> ModelVersion:
    """Версия модели для реестра."""
    return ModelVersion(
        model_version_id=version_id,
        model_family="logreg_elasticnet",
        feature_schema_version="v1",
        embedding_model="intfloat/multilingual-e5-base",
        dataset_version="ds-2026.09.15-v1",
        artifact_path="active",
        artifact_sha256="a" * 64,
        trained_at=NOW,
        metrics=ModelMetrics(0.84, 0.82, 0.8, 0.81, 0.88, 0.5, 44, "holdout+cv"),
    )


def make_draft(key: str = "job-1:analyze") -> AnalysisDraft:
    """Черновик анализа."""
    return AnalysisDraft(
        idempotency_key=key,
        collection_id=COLLECTION_ID,
        query_text="нейроморфные вычисления",
        model_version_id="wsclf-2026.09.15-1",
        params=AnalysisParams.from_request(0.5),
    )


def make_candidate(index: int, decision: Decision, rank: int) -> Candidate:
    """Кандидат с полным набором признаков реестра (25 строк EAV)."""
    features = tuple(
        FeatureContribution(f"feature_{number}", 0.1 * number, 0.01 * number, f"Признак {number}",
                            FeatureDirection.NEUTRAL)
        for number in range(25)
    )
    return Candidate(
        cluster_index=index,
        title_auto=f"кандидат {index}",
        keyphrases=("фраза", "вторая фраза"),
        score=0.9 if decision is Decision.WEAK_SIGNAL else 0.2,
        decision=decision,
        decision_reason=(
            DecisionReason.MODEL_SCORE
            if decision is Decision.WEAK_SIGNAL
            else DecisionReason.MATURITY_LEXICON
        ),
        decision_explanation_ru="объяснение решения",
        features=features,
        documents=(
            ClusterDocument(DOCUMENT_ID, 0.8, True, "сниппет", SourceType.NEWS, TrustLevel.HIGH, 2026),
        ),
        document_count=1,
        query_relevance=0.7,
        rank=rank,
    )


def test_migration_creates_schema(pool) -> None:  # noqa: ANN001 - фикстура
    """Миграция создаёт семь таблиц схемы."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM information_schema.tables WHERE table_schema = 'analyzer'"
        )
        assert cur.fetchone()["total"] == 7


def test_analysis_idempotency(pool) -> None:  # noqa: ANN001 - фикстура
    """Повторный запрос с тем же ключом возвращает существующий анализ."""
    repository = PostgresAnalysisRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    first, existed = repository.create_if_absent(make_draft())
    assert existed is False
    second, existed = repository.create_if_absent(make_draft())
    assert existed is True
    assert first.analysis_id == second.analysis_id


def test_claim_lease_and_heartbeat(pool) -> None:  # noqa: ANN001 - фикстура
    """Аренда захватывается один раз; heartbeat продлевает её только владельцу."""
    repository = PostgresAnalysisRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    repository.create_if_absent(make_draft())
    claimed = repository.claim_next("worker-1", 60)
    assert claimed is not None
    assert repository.claim_next("worker-2", 60) is None
    assert repository.heartbeat(claimed.analysis_id, "worker-1", 60).alive is True
    assert repository.heartbeat(claimed.analysis_id, "worker-2", 60).alive is False


def test_save_results_is_transactional(pool) -> None:  # noqa: ANN001 - фикстура
    """Кандидаты, признаки, документы и статус пишутся одной транзакцией."""
    analyses = PostgresAnalysisRepository(pool)
    candidates = PostgresCandidateRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    analyses.create_if_absent(make_draft())
    analysis = analyses.claim_next("worker-1", 60)
    rows = [make_candidate(0, Decision.WEAK_SIGNAL, 1), make_candidate(1, Decision.MATURE, 0)]
    saved = candidates.save_results(
        analysis.analysis_id, "worker-1", rows, AnalysisStats(documents_input=10), NOW
    )
    assert saved is True
    assert analyses.get(analysis.analysis_id).status is OperationStatus.COMPLETED
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM candidate_features")
        assert cur.fetchone()["total"] == 50  # 25 признаков × 2 кандидата


def test_save_results_rejects_foreign_owner(pool) -> None:  # noqa: ANN001 - фикстура
    """Чужая аренда не может записать результат: данные не появляются."""
    analyses = PostgresAnalysisRepository(pool)
    candidates = PostgresCandidateRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    analyses.create_if_absent(make_draft())
    analysis = analyses.claim_next("worker-1", 60)
    saved = candidates.save_results(
        analysis.analysis_id, "worker-2", [make_candidate(0, Decision.WEAK_SIGNAL, 1)],
        AnalysisStats(), NOW,
    )
    assert saved is False
    rows, total = candidates.list_candidates(
        analysis.analysis_id, include_excluded=True, after=None, limit=10
    )
    assert total == 0
    assert rows == []


def test_second_attempt_replaces_candidates(pool) -> None:  # noqa: ANN001 - фикстура
    """Повторный прогон удаляет кандидатов прошлой попытки (нет смешанного состояния)."""
    analyses = PostgresAnalysisRepository(pool)
    candidates = PostgresCandidateRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    analyses.create_if_absent(make_draft())
    analysis = analyses.claim_next("worker-1", 60)
    candidates.save_results(
        analysis.analysis_id, "worker-1", [make_candidate(0, Decision.WEAK_SIGNAL, 1)],
        AnalysisStats(), NOW,
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE analyses SET status = 'RUNNING', lease_owner = 'worker-1', "
            "lease_expires_at = now() + interval '1 minute' WHERE analysis_id = %s",
            (analysis.analysis_id,),
        )
    candidates.save_results(
        analysis.analysis_id, "worker-1",
        [make_candidate(5, Decision.WEAK_SIGNAL, 1), make_candidate(6, Decision.MATURE, 0)],
        AnalysisStats(), NOW,
    )
    rows, total = candidates.list_candidates(
        analysis.analysis_id, include_excluded=True, after=None, limit=10
    )
    assert total == 2
    assert {row.cluster_index for row in rows} == {5, 6}


def test_embedding_cache_invalidation(pool) -> None:  # noqa: ANN001 - фикстура
    """Кеш обновляется при изменении content_hash."""
    cache = PostgresEmbeddingCache(pool)
    # Схема допускает только размерности реальных эмбеддеров: CHECK (dims IN (384, 768, 1024))
    # и CHECK (cardinality(vector) = dims). 384 — размерность multilingual-e5-small.
    dims = 384
    vector = np.ones(dims, dtype=np.float32)
    cache.put_many([CachedEmbedding(DOCUMENT_ID, "hash-1", vector)], "e5", dims)
    stored = cache.get_many([DOCUMENT_ID], "e5")[DOCUMENT_ID]
    assert stored.content_hash == "hash-1"
    cache.put_many([CachedEmbedding(DOCUMENT_ID, "hash-2", vector * 2)], "e5", dims)
    stored = cache.get_many([DOCUMENT_ID], "e5")[DOCUMENT_ID]
    assert stored.content_hash == "hash-2"
    assert float(stored.vector[0]) == 2.0


def test_only_one_active_model(pool) -> None:  # noqa: ANN001 - фикстура
    """Активной остаётся ровно одна версия модели (частичный уникальный индекс)."""
    repository = PostgresModelVersionRepository(pool)
    repository.activate(make_version("wsclf-2026.09.15-1"))
    repository.activate(make_version("wsclf-2026.09.16-1"))
    assert repository.get_active().model_version_id == "wsclf-2026.09.16-1"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM model_versions WHERE is_active")
        assert cur.fetchone()["total"] == 1


def test_release_expired_leases(pool) -> None:  # noqa: ANN001 - фикстура
    """Анализ с истёкшей арендой возвращается в очередь."""
    analyses = PostgresAnalysisRepository(pool)
    PostgresModelVersionRepository(pool).activate(make_version())
    analyses.create_if_absent(make_draft())
    analysis = analyses.claim_next("worker-1", 60)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE analyses SET lease_expires_at = now() - interval '1 minute' WHERE analysis_id = %s",
            (analysis.analysis_id,),
        )
    assert analyses.release_expired_leases() == 1
    assert analyses.get(analysis.analysis_id).status is OperationStatus.PENDING
