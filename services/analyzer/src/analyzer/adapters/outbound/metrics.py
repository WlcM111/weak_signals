"""Реализация приёмника метрик analyzer на prometheus_client."""

from __future__ import annotations

from analyzer.domain.values import Decision, OperationStatus
from ws_common.metrics import (
    ANALYSES,
    ANALYSIS_STEP_DURATION,
    CANDIDATES,
    EMBEDDING_CACHE_HITS,
    EMBEDDINGS_COMPUTED,
    MODEL_INFO,
)


class PrometheusMetrics:
    """Публикует метрики анализа в общий реестр `ws_common.metrics`."""

    def embeddings_computed(self, count: int) -> None:
        """`ws_embeddings_computed_total`."""
        if count:
            EMBEDDINGS_COMPUTED.inc(count)

    def embedding_cache_hits(self, count: int) -> None:
        """`ws_embedding_cache_hits_total`."""
        if count:
            EMBEDDING_CACHE_HITS.inc(count)

    def step_duration(self, step: str, seconds: float) -> None:
        """`ws_analysis_duration_seconds{step}`."""
        ANALYSIS_STEP_DURATION.labels(step).observe(seconds)

    def candidate_decided(self, decision: Decision) -> None:
        """`ws_candidates_total{decision}`."""
        CANDIDATES.labels(decision.value).inc()

    def analysis_finished(self, status: OperationStatus) -> None:
        """`ws_analyses_total{status}`."""
        ANALYSES.labels(status.value).inc()

    def model_activated(self, model_version_id: str) -> None:
        """`ws_model_info{version}` = 1 для активной версии."""
        MODEL_INFO.labels(model_version_id).set(1)
