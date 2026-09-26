"""Стадия 4 в режиме отбора rubric: пул → состав источников → предфильтр → рубричная оценка LLM →
балл «стадия + тренд» → ТОП-N → пакетная доводка карточек → запись (§ ТЗ: ТОП-15, объяснение, источники).

Если рубричная оценка недоступна целиком, стадия выполняется прежним способом (narrate.run_narrate с режимом
legacy): до этого момента ничего не записывается, поэтому переход не создаёт дублей. Карточки, которые пакетная
доводка не приняла, строятся прежним генератором нарратива или экстрактивно — выдача не теряется.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from orchestrator.application.dto import (
    CandidateView,
    DocumentView,
    FinalizedCardView,
    FinalizeRequestCard,
    JudgeVerdictView,
    RubricRequestItem,
)
from orchestrator.application.ports import AnalyzerClient, CollectorClient, InsightClient, ResultRepository
from orchestrator.application.stages import rubric
from orchestrator.application.stages.narrate import (
    MAX_EVIDENCE,
    NarrateConfig,
    NarrateOutcome,
    _narrate_candidate,
    build_sources,
    collect_candidates,
    run_narrate,
)
from orchestrator.domain.entities import ExcludedCandidate, ResultItem
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import Decision, NarrativeStatus
from ws_common.logging import get_logger

DOCUMENTS_BATCH = 150
log = get_logger("orchestrator.stage.narrate_rubric")


async def run_narrate_rubric(
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
    """ТОП-N по рубрике и баллу стадия + тренд; без оценки LLM — прежняя стадия целиком."""
    legacy = replace(config, selection_mode="legacy")
    if insight is None or (config.llm_allowed is not None and not config.llm_allowed()):
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    weak, excluded = await collect_candidates(analyzer, analysis_id, check)
    pool, rest = rubric.pool_candidates(weak, excluded, config.rubric_pool)
    documents = await _load_all(collector, pool, check)
    compositions = {c.candidate_id: rubric.composition_of(_docs(c, documents)) for c in pool}
    prefiltered: list[tuple[CandidateView, tuple[Decision, str, str]]] = []
    judged_pool: list[CandidateView] = []
    for candidate in pool:
        verdict = rubric.prefilter(compositions[candidate.candidate_id])
        (prefiltered.append((candidate, verdict)) if verdict else judged_pool.append(candidate))
    verdicts = await _judge(insight, query_text, judged_pool, documents, compositions)
    if not verdicts:
        log.warning("stage.narrate_rubric.judge_unavailable", pool=len(judged_pool))
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    outcome = NarrateOutcome(candidates_found=len(weak) + len(excluded))
    selected = [c for c in judged_pool if c.candidate_id in verdicts and verdicts[c.candidate_id].code == "R"]
    rejected = [c for c in judged_pool if c.candidate_id in verdicts and verdicts[c.candidate_id].code != "R"]
    unjudged = [c for c in judged_pool if c.candidate_id not in verdicts]
    ranked = rubric.rank_selected(selected, verdicts)
    shown, overflow = ranked[: config.top_n], ranked[config.top_n :]
    outcome.weak_signals_total = len(ranked)
    outcome.weak_signals_confident = sum(1 for c in ranked if verdicts[c.candidate_id].confidence >= 0.75)
    outcome.judge_rejected = len(rejected) + len(prefiltered)
    outcome.excluded_written = await results.add_excluded(job_id, _excluded_rows(
        rest, prefiltered, rejected, unjudged, overflow, verdicts))
    finalized = await _finalize(insight, query_text, shown, documents, verdicts, config)
    final: list[tuple[CandidateView, int, int]] = []
    for candidate in shown:
        verdict = verdicts[candidate.candidate_id]
        card = finalized.get(candidate.candidate_id)
        final.append((candidate, card.stage if card else verdict.stage, card.trend if card else verdict.trend))
    order = {c.candidate_id: i for i, c in enumerate(shown)}
    final.sort(key=lambda row: (-(row[1] + row[2]), -verdicts[row[0].candidate_id].confidence, order[row[0].candidate_id]))
    for position, (candidate, stage, trend) in enumerate(final, 1):
        await check()
        item = await _build_item(insight, job_id, query_text, position, candidate, verdicts[candidate.candidate_id],
                                 finalized.get(candidate.candidate_id), _docs(candidate, documents),
                                 compositions[candidate.candidate_id], stage, trend, config, outcome)
        await results.add_item(item)
        outcome.items_written += 1
        if item.narrative_status is NarrativeStatus.GENERATED:
            outcome.narratives_generated += 1
        else:
            outcome.narratives_fallback += 1
    log.info("stage.narrate_rubric", pool=len(pool), prefiltered=len(prefiltered), judged=len(verdicts),
             accepted=len(selected), shown=outcome.items_written, finalized=len(finalized),
             generated=outcome.narratives_generated, fallback=outcome.narratives_fallback)
    return outcome


def _docs(candidate: CandidateView, documents: dict[str, DocumentView]) -> tuple[DocumentView, ...]:
    """Документы кандидата в порядке доказательств analyzer."""
    return tuple(documents[e.document_id] for e in candidate.evidence[:MAX_EVIDENCE] if e.document_id in documents)


async def _load_all(
    collector: CollectorClient, pool: list[CandidateView], check: Callable[[], Awaitable[None]]
) -> dict[str, DocumentView]:
    """Документы всего пула пачками (GetDocuments принимает до 200 идентификаторов)."""
    identifiers = list(dict.fromkeys(e.document_id for c in pool for e in c.evidence[:MAX_EVIDENCE]))
    found: dict[str, DocumentView] = {}
    for start in range(0, len(identifiers), DOCUMENTS_BATCH):
        await check()
        try:
            batch = await collector.get_documents(identifiers[start : start + DOCUMENTS_BATCH])
        except UpstreamUnavailable as error:
            log.warning("stage.narrate_rubric.documents", error=str(error)[:200])
            continue
        found.update({document.document_id: document for document in batch})
    return found


async def _judge(
    insight: InsightClient, query_text: str, pool: list[CandidateView], documents: dict[str, DocumentView],
    compositions: dict[str, rubric.Composition],
) -> dict[str, JudgeVerdictView]:
    """Рубричная оценка пула; недоступность — пустой словарь."""
    if not pool:
        return {}
    items = [RubricRequestItem(c, _docs(c, documents), rubric.composition_ru(compositions[c.candidate_id])) for c in pool]
    try:
        return await insight.judge_rubric(query_text, items)
    except (UpstreamUnavailable, StageFailed) as error:
        log.warning("stage.narrate_rubric.judge_failed", error=str(error)[:200])
        return {}


async def _finalize(
    insight: InsightClient, query_text: str, shown: list[CandidateView], documents: dict[str, DocumentView],
    verdicts: dict[str, JudgeVerdictView], config: NarrateConfig,
) -> dict[str, FinalizedCardView]:
    """Пакетная доводка показанных карточек; при отказе — пустой словарь (карточки строятся прежним способом)."""
    if not shown or not config.finalize_enabled or (config.llm_allowed is not None and not config.llm_allowed()):
        return {}
    cards = [FinalizeRequestCard(c, _docs(c, documents), verdicts[c.candidate_id].stage,
                                 verdicts[c.candidate_id].trend, verdicts[c.candidate_id].reason_ru) for c in shown]
    try:
        return await insight.finalize_cards(query_text, cards)
    except (UpstreamUnavailable, StageFailed) as error:
        log.warning("stage.narrate_rubric.finalize_failed", error=str(error)[:200])
        return {}


def _excluded_rows(
    rest: list[CandidateView],
    prefiltered: list[tuple[CandidateView, tuple[Decision, str, str]]],
    rejected: list[CandidateView],
    unjudged: list[CandidateView],
    overflow: list[CandidateView],
    verdicts: dict[str, JudgeVerdictView],
) -> list[ExcludedCandidate]:
    """Исключённые кандидаты с причинами (ТЗ: причины исключения зрелых и нерелевантных кандидатов)."""
    rows: list[ExcludedCandidate] = []

    def add(candidate: CandidateView, decision: Decision, reason: str, text: str, score: float) -> None:
        rows.append(ExcludedCandidate(candidate.candidate_id, candidate.title_auto[:200], round(score, 4), decision,
                                      reason, text[:500], candidate.document_count))

    for candidate in rest:
        decision = candidate.decision if candidate.decision is not Decision.WEAK_SIGNAL else Decision.INSUFFICIENT_EVIDENCE
        add(candidate, decision, candidate.decision_reason or "RUBRIC_POOL_LIMIT",
            candidate.decision_explanation_ru or "Не вошёл в пул рубричной оценки.", candidate.score)
    for candidate, (decision, reason, text) in prefiltered:
        add(candidate, decision, reason, text, candidate.score)
    for candidate in rejected:
        verdict = verdicts[candidate.candidate_id]
        add(candidate, rubric.CODE_DECISION.get(verdict.code, Decision.INSUFFICIENT_EVIDENCE), f"RUBRIC_{verdict.code}",
            f"Рубричная оценка: {rubric.CODE_RU.get(verdict.code, verdict.code)}. {verdict.reason_ru}",
            verdict.confidence)
    for candidate in unjudged:
        add(candidate, Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_UNJUDGED",
            "Рубричная оценка не получена (пачка отклонена моделью); кандидат не показан.", candidate.score)
    for candidate in overflow:
        verdict = verdicts[candidate.candidate_id]
        add(candidate, Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_BELOW_TOP_N",
            f"Слабый сигнал вне ТОП-N по баллу стадия + тренд ({rubric.priority(verdict)} из 7). {verdict.reason_ru}",
            verdict.confidence)
    return rows


async def _build_item(
    insight: InsightClient | None, job_id: str, query_text: str, position: int, candidate: CandidateView,
    verdict: JudgeVerdictView, card: FinalizedCardView | None, documents: tuple[DocumentView, ...],
    composition: rubric.Composition, stage: int, trend: int, config: NarrateConfig, outcome: NarrateOutcome,
) -> ResultItem:
    """Элемент выдачи: тексты пакетной доводки или прежнего генератора, рубрика — в объяснении и предикторах."""
    status = rubric.status_explanation(verdict, stage, trend)
    features = rubric.feature_rows(rubric.rubric_features(verdict, composition, stage, trend), candidate)
    if card is not None:
        companies = f" Компании и организации из источников: {', '.join(card.companies)}." if card.companies else ""
        reasons = " ".join(part for part in (card.stage_reason_ru, card.trend_reason_ru) if part)
        return ResultItem(
            job_id=job_id, rank=position, candidate_id=candidate.candidate_id, title_ru=card.title_ru[:200],
            title_auto=candidate.title_auto[:200], score=round(verdict.confidence, 4), decision_reason="RUBRIC_R",
            decision_explanation_ru=status, description_ru=card.description_ru, advantage_ru=card.advantage_ru,
            case_example_ru=card.case_example_ru, case_document_id=card.case_document_id,
            explanation_ru=f"{card.why_ru}{companies} {reasons} {status}".strip(),
            narrative_status=NarrativeStatus.GENERATED, llm_provider=card.llm_provider, llm_model=card.llm_model,
            prompt_version=card.prompt_version, document_count=candidate.document_count, features=features,
            sources=build_sources(candidate, {d.document_id: d for d in documents}, card.source_summaries),
            predicted_stage=stage, predicted_trend=trend,
        )
    use_llm = config.llm_allowed is None or config.llm_allowed()
    if not use_llm:
        outcome.deadline_fallbacks += 1
    narrative = await _narrate_candidate(insight if use_llm else None, job_id, query_text, candidate,
                                         {d.document_id: d for d in documents}, config)
    return ResultItem(
        job_id=job_id, rank=position, candidate_id=candidate.candidate_id,
        title_ru=(narrative.title_ru or candidate.title_auto)[:200], title_auto=candidate.title_auto[:200],
        score=round(verdict.confidence, 4), decision_reason="RUBRIC_R", decision_explanation_ru=status,
        description_ru=narrative.description_ru, advantage_ru=narrative.advantage_ru,
        case_example_ru=narrative.case_example_ru, case_document_id=narrative.case_document_id,
        explanation_ru=f"{narrative.explanation_ru} {status}".strip(),
        narrative_status=NarrativeStatus(narrative.status), llm_provider=narrative.llm_provider,
        llm_model=narrative.llm_model, prompt_version=narrative.prompt_version,
        document_count=candidate.document_count, features=features,
        sources=build_sources(candidate, {d.document_id: d for d in documents}, narrative.source_summaries),
        predicted_stage=stage, predicted_trend=trend,
    )
