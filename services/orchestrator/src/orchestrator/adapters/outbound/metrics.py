"""Метрики orchestrator в общий реестр `ws_common.metrics`."""

from __future__ import annotations

from orchestrator.domain.values import JobStatus
from ws_common.metrics import (
    HTTP_REQUESTS,
    JOB_DURATION,
    JOBS,
    QUEUE_PENDING,
    UPSTREAM_CALLS,
)


class PrometheusMetrics:
    """Реализация порта `MetricsSink`."""

    def job_finished(self, status: JobStatus) -> None:
        """`ws_jobs_total{status}`."""
        JOBS.labels(status.value).inc()

    def stage_duration(self, stage: str, seconds: float) -> None:
        """`ws_job_duration_seconds{stage}`."""
        JOB_DURATION.labels(stage).observe(seconds)

    def queue_pending(self, count: int) -> None:
        """`ws_queue_pending`."""
        QUEUE_PENDING.set(count)

    def http_request(self, path: str, status: int) -> None:
        """`ws_http_requests_total{path,status}`."""
        HTTP_REQUESTS.labels(path, str(status)).inc()

    def upstream_call(self, rpc: str, code: str) -> None:
        """`ws_upstream_calls_total{rpc,code}`."""
        UPSTREAM_CALLS.labels(rpc, code).inc()
