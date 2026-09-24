"""Стадия 5 — статистика задания и выбор итогового статуса (§7.5 HANDOFF)."""

from __future__ import annotations

from orchestrator.application.dto import AnalysisView, CollectionView, ExpansionView
from orchestrator.application.stages.narrate import NarrateOutcome
from orchestrator.domain.rules import Completion, decide_completion
from orchestrator.domain.values import JobStats


def build_stats(
    collection: CollectionView | None,
    analysis: AnalysisView | None,
    outcome: NarrateOutcome,
    expansion: ExpansionView | None,
    durations: dict[str, int],
) -> JobStats:
    """Собирает `job_stats` из данных стадий; отсутствующие источники дают нули."""
    return JobStats(
        http_requests_total=collection.http_requests_total if collection else 0,
        sources_processed=collection.adapters_completed if collection else 0,
        documents_collected=collection.documents_total if collection else 0,
        candidates_found=outcome.candidates_found,
        weak_signals_total=outcome.weak_signals_total,
        weak_signals_confident=outcome.weak_signals_confident,
        collect_ms=durations.get("collect"),
        analyze_ms=durations.get("analyze"),
        narrate_ms=durations.get("narrate"),
        narratives_generated=outcome.narratives_generated,
        narratives_fallback=outcome.narratives_fallback,
        model_version_id=analysis.model_version_id if analysis else "",
        expand_used_fallback=expansion.used_fallback if expansion else None,
    )


def decide_final_status(
    outcome: NarrateOutcome, requested_top_n: int, collection: CollectionView | None
) -> Completion:
    """Итоговый статус задания и сообщение о неполноте результата."""
    return decide_completion(
        items_written=outcome.items_written,
        requested_top_n=requested_top_n,
        narratives_fallback=outcome.narratives_fallback,
        collection_status=collection.status if collection else "UNKNOWN",
        failed_adapters=collection.failed_adapters if collection else (),
        deadline_reached=outcome.deadline_fallbacks > 0,
    )
