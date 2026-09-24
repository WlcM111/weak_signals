"""Реализация приёмника метрик на prometheus_client (порт `MetricsSink`)."""

from __future__ import annotations

from collector.domain.values import OperationStatus, SourceKey
from ws_common.metrics import ADAPTER_REQUESTS, COLLECTION_DURATION, COLLECTIONS, DOCUMENTS_NEW, RATE_LIMIT_WAITS


class PrometheusMetrics:
    """Публикует метрики сбора в общий реестр `ws_common.metrics`."""

    def documents_new(self, source_key: SourceKey, count: int) -> None:
        """`ws_documents_new_total{source}`."""
        if count:
            DOCUMENTS_NEW.labels(source_key.value).inc(count)

    def adapter_finished(self, source_key: SourceKey, status: OperationStatus) -> None:
        """`ws_adapter_requests_total{source,status}`."""
        ADAPTER_REQUESTS.labels(source_key.value, status.value).inc()

    def collection_finished(self, mode: str, status: OperationStatus, duration_seconds: float) -> None:
        """`ws_collection_duration_seconds{mode}` и `ws_collections_total{status}`."""
        COLLECTION_DURATION.labels(mode).observe(duration_seconds)
        COLLECTIONS.labels(status.value).inc()

    def rate_limit_wait(self, source_key: SourceKey) -> None:
        """`ws_rate_limit_waits_total{source}`."""
        RATE_LIMIT_WAITS.labels(source_key.value).inc()
