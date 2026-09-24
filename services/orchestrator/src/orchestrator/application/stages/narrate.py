"""Стадия 4 — сборка элементов выдачи: доказательства, нарратив, транзакционная запись (§7.4).

Элементы пишутся по одному сразу после генерации: интерфейс показывает прогрессивную выдачу,
а повторная попытка задания не перезаписывает уже готовые элементы (`ON CONFLICT DO NOTHING`).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from orchestrator.application.dto import CandidateView, DocumentView
from orchestrator.application.ports import (
    AnalyzerClient,
    CollectorClient,
    InsightClient,
    ResultRepository,
)
from orchestrator.domain.entities import ExcludedCandidate, FeatureRow, ResultItem, SourceRow
from orchestrator.domain.errors import UpstreamUnavailable
from orchestrator.domain.values import Decision, NarrativeStatus, SummaryKind
from ws_common.logging import get_logger

CANDIDATES_PAGE_SIZE = 50
MAX_EVIDENCE = 8
MAX_SNIPPET = 600
MAX_SUMMARY = 400
_SENTENCE_END_RE = re.compile(r"[.!?…]\s")
log = get_logger("orchestrator.stage.narrate")


@dataclass(frozen=True, slots=True)
class NarrateConfig:
    """Параметры стадии нарратива."""

    top_n: int = 15
    evidence_text_max_chars: int = 2000
    prompt_version: str = "insight_v1"
    # Хватает ли времени до срока задания на ещё один вызов LLM; None — срок не ограничивает.
    llm_allowed: Callable[[], bool] | None = None


@dataclass(slots=True)
class NarrateOutcome:
    """Итог стадии: сколько элементов записано и как они получены."""

    items_written: int = 0
    narratives_generated: int = 0
    narratives_fallback: int = 0
    # Карточки, собранные экстрактивно потому, что до срока задания не хватало времени на LLM.
    deadline_fallbacks: int = 0
    excluded_written: int = 0
    candidates_found: int = 0
    weak_signals_total: int = 0
    weak_signals_confident: int = 0
    missing_documents: list[str] = field(default_factory=list)


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """Усекает текст по границе предложения, не превышая лимит (§7.4 HANDOFF)."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    window = cleaned[: max_chars + 1]
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(window)]
    if ends and ends[-1] > max_chars // 2:
        return window[: ends[-1]].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window[:max_chars]).strip()


async def collect_candidates(
    analyzer: AnalyzerClient, analysis_id: str, check: Callable[[], Awaitable[None]]
) -> tuple[list[CandidateView], list[CandidateView]]:
    """Читает всех кандидатов постранично и разделяет на слабые сигналы и исключённых."""
    weak: list[CandidateView] = []
    excluded: list[CandidateView] = []
    page_token = ""
    while True:
        await check()
        rows, page_token = await analyzer.list_candidates(
            analysis_id, include_excluded=True, page_size=CANDIDATES_PAGE_SIZE, page_token=page_token
        )
        for candidate in rows:
            (weak if candidate.decision is Decision.WEAK_SIGNAL else excluded).append(candidate)
        if not page_token:
            break
    weak.sort(key=lambda item: item.rank)
    return weak, excluded


def build_sources(
    candidate: CandidateView, documents: dict[str, DocumentView], summaries: dict[str, tuple[str, str]]
) -> tuple[SourceRow, ...]:
    """Источники элемента: порядок доказательств analyzer, резюме от insight или сниппет."""
    rows: list[SourceRow] = []
    position = 0
    for evidence in candidate.evidence:
        document = documents.get(evidence.document_id)
        if document is None:
            continue
        position += 1
        summary_text, summary_kind = summaries.get(
            evidence.document_id,
            (
                truncate_at_sentence(evidence.snippet or document.text, MAX_SUMMARY),
                SummaryKind.ORIGINAL_RU.value
                if document.language_code == "ru"
                else SummaryKind.EXTRACTIVE.value,
            ),
        )
        rows.append(
            SourceRow(
                position=position,
                document_id=document.document_id,
                title=document.title,
                url=document.url,
                published_at=document.published_at,
                source_type=document.source_type,
                source_key=document.source_key,
                language_code=document.language_code,
                trust_level=document.trust_level,
                summary_ru=truncate_at_sentence(summary_text, MAX_SUMMARY),
                summary_kind=SummaryKind(summary_kind),
                snippet=truncate_at_sentence(evidence.snippet, MAX_SNIPPET),
                similarity=evidence.similarity,
            )
        )
    return tuple(rows)


def build_features(candidate: CandidateView) -> tuple[FeatureRow, ...]:
    """Все признаки кандидата в порядке убывания модуля вклада (порядок задаёт analyzer)."""
    return tuple(
        FeatureRow(
            feature_name=feature.feature_name,
            label_ru=feature.label_ru,
            value=feature.value,
            contribution=feature.contribution,
            direction=feature.direction,
            display_order=index,
        )
        for index, feature in enumerate(candidate.features, start=1)
    )


async def run_narrate(
    *,
    analyzer: AnalyzerClient,
    collector: CollectorClient,
    insight: InsightClient | None,
    results: ResultRepository,
    job_id: str,
    query_text: str,
    analysis_id: str,
    config: NarrateConfig,
    check: Callable[[], Awaitable[None]],
) -> NarrateOutcome:
    """Формирует ТОП-N элементов и записывает исключённых кандидатов с причинами."""
    outcome = NarrateOutcome()
    weak, excluded = await collect_candidates(analyzer, analysis_id, check)
    outcome.candidates_found = len(weak) + len(excluded)
    outcome.weak_signals_total = len(weak)
    outcome.weak_signals_confident = sum(1 for item in weak if item.score >= 0.75)
    outcome.excluded_written = await results.add_excluded(
        job_id,
        [
            ExcludedCandidate(
                candidate_id=item.candidate_id,
                title_auto=item.title_auto,
                score=item.score,
                decision=item.decision,
                decision_reason=item.decision_reason,
                decision_explanation_ru=item.decision_explanation_ru,
                document_count=item.document_count,
            )
            for item in excluded
        ],
    )
    for candidate in weak[: config.top_n]:
        await check()
        documents = await _load_documents(collector, candidate, outcome)
        use_llm = config.llm_allowed is None or config.llm_allowed()
        if not use_llm:
            if outcome.deadline_fallbacks == 0:
                log.warning("stage.narrate.deadline", candidate_id=candidate.candidate_id, rank=candidate.rank)
            outcome.deadline_fallbacks += 1
        narrative = await _narrate_candidate(
            insight if use_llm else None, job_id, query_text, candidate, documents, config
        )
        item = ResultItem(
            job_id=job_id,
            rank=candidate.rank,
            candidate_id=candidate.candidate_id,
            title_ru=(narrative.title_ru or candidate.title_auto)[:200],
            title_auto=candidate.title_auto,
            score=candidate.score,
            decision_reason=candidate.decision_reason,
            decision_explanation_ru=candidate.decision_explanation_ru,
            description_ru=narrative.description_ru,
            advantage_ru=narrative.advantage_ru,
            case_example_ru=narrative.case_example_ru,
            case_document_id=narrative.case_document_id,
            explanation_ru=narrative.explanation_ru or candidate.decision_explanation_ru,
            narrative_status=NarrativeStatus(narrative.status),
            llm_provider=narrative.llm_provider,
            llm_model=narrative.llm_model,
            prompt_version=narrative.prompt_version,
            document_count=candidate.document_count,
            features=build_features(candidate),
            sources=build_sources(candidate, documents, narrative.source_summaries),
            predicted_stage=candidate.predicted_stage,
            predicted_trend=candidate.predicted_trend,
        )
        await results.add_item(item)
        outcome.items_written += 1
        if item.narrative_status is NarrativeStatus.GENERATED:
            outcome.narratives_generated += 1
        else:
            outcome.narratives_fallback += 1
    log.info(
        "stage.narrate",
        items=outcome.items_written,
        generated=outcome.narratives_generated,
        fallback=outcome.narratives_fallback,
        excluded=outcome.excluded_written,
    )
    return outcome


async def _load_documents(
    collector: CollectorClient, candidate: CandidateView, outcome: NarrateOutcome
) -> dict[str, DocumentView]:
    """Загружает доказательные документы кандидата; недоступность collector не срывает элемент."""
    identifiers = [item.document_id for item in candidate.evidence][:MAX_EVIDENCE]
    if not identifiers:
        return {}
    try:
        documents = await collector.get_documents(identifiers)
    except UpstreamUnavailable as error:
        log.warning("stage.narrate.documents", candidate_id=candidate.candidate_id, error=str(error))
        return {}
    found = {document.document_id: document for document in documents}
    outcome.missing_documents.extend(key for key in identifiers if key not in found)
    return found


async def _narrate_candidate(
    insight: InsightClient | None,
    job_id: str,
    query_text: str,
    candidate: CandidateView,
    documents: dict[str, DocumentView],
    config: NarrateConfig,
):  # noqa: ANN201 - NarrativeView
    """Генерирует нарратив; при недоступности insight собирает экстрактивный вариант."""
    evidence = [
        DocumentView(
            document_id=document.document_id,
            title=document.title,
            url=document.url,
            text=truncate_at_sentence(document.text, config.evidence_text_max_chars),
            language_code=document.language_code,
            source_type=document.source_type,
            source_key=document.source_key,
            trust_level=document.trust_level,
            published_at=document.published_at,
        )
        for document in documents.values()
    ]
    if insight is not None and evidence:
        key = f"{job_id}:{candidate.candidate_id}:insight:{config.prompt_version}"
        try:
            return await insight.generate_insight(
                key, candidate, query_text, evidence, config.prompt_version
            )
        except Exception as error:  # noqa: BLE001 - отказ нарратива понижает статус, но не рушит задание
            log.warning("stage.narrate.insight", candidate_id=candidate.candidate_id, error=str(error))
    return extractive_narrative(candidate, evidence, config.prompt_version)


def extractive_narrative(
    candidate: CandidateView, evidence: Sequence[DocumentView], prompt_version: str
):  # noqa: ANN201 - NarrativeView
    """Экстрактивный нарратив без LLM: тексты берутся из доказательств и объяснения analyzer.

    Требование ТЗ — не формировать выдачу на знаниях языковой модели без подтверждённого поиска;
    здесь верно и обратное: без LLM выдача всё равно собирается, но помечается как экстрактивная.
    """
    from orchestrator.application.dto import NarrativeView  # noqa: PLC0415 - избегаем цикла импорта

    first = evidence[0] if evidence else None
    description = truncate_at_sentence(first.text, 800) if first else candidate.title_auto
    top_features = ", ".join(
        f"{feature.label_ru.lower()} ({feature.contribution:+.2f})" for feature in candidate.features[:3]
    )
    return NarrativeView(
        title_ru=candidate.title_auto,
        description_ru=description,
        advantage_ru=(
            "Потенциальное преимущество не сформулировано: нарратив собран без генеративной модели, "
            f"по доказательным источникам ({len(evidence)} шт.)."
        ),
        case_example_ru=(
            f"{first.title} — {truncate_at_sentence(first.text, 300)}" if first else "Кейс-пример недоступен."
        ),
        explanation_ru=(
            f"{candidate.decision_explanation_ru} Ключевые признаки: {top_features}."
            if top_features
            else candidate.decision_explanation_ru
        ),
        status=NarrativeStatus.FALLBACK_EXTRACTIVE.value,
        llm_provider="none",
        llm_model="",
        prompt_version=prompt_version,
        case_document_id=first.document_id if first else "",
        source_summaries={},
    )
