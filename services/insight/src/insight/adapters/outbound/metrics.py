"""Метрики insight в общий реестр `ws_common.metrics`."""

from __future__ import annotations

from ws_common.metrics import (
    INSIGHT_STATUS,
    LLM_CALLS,
    LLM_DAILY_TOKENS,
    LLM_LATENCY,
    LLM_TOKENS,
    PROVIDER_HEALTHY,
)


class PrometheusMetrics:
    """Реализация порта `MetricsSink`."""

    def llm_call(self, provider: str, status: str) -> None:
        """`ws_llm_calls_total{provider,status}`."""
        LLM_CALLS.labels(provider, status).inc()

    def llm_tokens(self, provider: str, kind: str, count: int) -> None:
        """`ws_llm_tokens_total{provider,kind}`."""
        if count:
            LLM_TOKENS.labels(provider, kind).inc(count)

    def llm_latency(self, provider: str, seconds: float) -> None:
        """`ws_llm_latency_seconds{provider}`."""
        LLM_LATENCY.labels(provider).observe(seconds)

    def insight_status(self, status: str) -> None:
        """`ws_insight_status_total{status}`."""
        INSIGHT_STATUS.labels(status).inc()

    def provider_healthy(self, provider: str, healthy: bool) -> None:
        """`ws_provider_healthy{provider}`."""
        PROVIDER_HEALTHY.labels(provider).set(1 if healthy else 0)

    def daily_tokens(self, count: int) -> None:
        """`ws_llm_daily_tokens`."""
        LLM_DAILY_TOKENS.set(count)
