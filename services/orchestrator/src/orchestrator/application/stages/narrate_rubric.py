"""Стадия 4 в режиме отбора rubric: пул → состав источников → предфильтр → рубричная оценка LLM →
калибровка стадии и тренда → балл «стадия + тренд» → ТОП-N → пакетная доводка → запись (ТЗ: ТОП-15 на русском).

Показываются только карточки, прошедшие пакетную доводку: все поля заполнены, тексты и резюме источников на
русском (ТЗ: «аналитическая выдача должна быть представлена на русском языке»). Карточка, которую доводка не
приняла, заменяется следующим кандидатом (не более двух раундов — срок задания ограничен). Если слабых
сигналов R меньше ТОП-N, добираются кандидаты с кодом U с пометкой «требует проверки»: по ответу заказчика,
в каждой категории слабых сигналов больше 15, а первичная проверка — попадание в них. Если рубричная оценка
недоступна целиком, выдача пустая с причинами в исключённых; прежняя стадия (английские экстрактивные
карточки) — только при WS_RUBRIC_LEGACY_FALLBACK=true.
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
    build_sources,
    collect_candidates,
    run_narrate,
)
from orchestrator.domain.entities import ExcludedCandidate, ResultItem
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import Decision, NarrativeStatus
from ws_common.logging import get_logger

DOCUMENTS_BATCH = 150
FINALIZE_ROUNDS = 2
FINALIZE_SOURCES = 5
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
    """ТОП-N полностью доведённых карточек по рубрике и баллу стадия + тренд."""
    legacy = replace(config, selection_mode="legacy")
    llm_ok = insight is not None and (config.llm_allowed is None or config.llm_allowed())
    if not llm_ok and config.rubric_legacy_fallback:
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    weak, excluded = await collect_candidates(analyzer, analysis_id, check)
    outcome = NarrateOutcome(candidates_found=len(weak) + len(excluded))
    pool, rest = rubric.pool_candidates(weak, excluded, config.rubric_pool)
    documents = await _load_all(collector, pool, check)
    compositions = {c.candidate_id: rubric.composition_of(_docs(c, documents)) for c in pool}
    prefiltered: list[tuple[CandidateView, tuple[Decision, str, str]]] = []
    judged_pool: list[CandidateView] = []
    for candidate in pool:
        verdict = rubric.prefilter(compositions[candidate.candidate_id])
        (prefiltered.append((candidate, verdict)) if verdict else judged_pool.append(candidate))
    raw = await _judge(insight, query_text, judged_pool, documents, compositions) if llm_ok else {}
    if not raw:
        log.warning("stage.narrate_rubric.judge_unavailable", pool=len(judged_pool))
        if config.rubric_legacy_fallback:
            return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                     job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                     check=check)
        outcome.excluded_written = await results.add_excluded(
            job_id, _excluded_rows(rest, prefiltered, [], judged_pool, [], [], {}))
        return outcome
    stage_map = rubric.parse_calibration(config.stage_calibration, 1, 4)
    trend_map = rubric.parse_calibration(config.trend_calibration, 1, 3)
    verdicts = {cid: replace(v, stage=stage_map.get(v.stage, v.stage), trend=trend_map.get(v.trend, v.trend))
                for cid, v in raw.items()}
    fill = config.rubric_fill_uncertain
    accepted = [c for c in judged_pool if c.candidate_id in verdicts and verdicts[c.candidate_id].code == "R"]
    uncertain = [c for c in judged_pool if c.candidate_id in verdicts and verdicts[c.candidate_id].code == "U"] if fill else []
    rejected = [c for c in judged_pool if c.candidate_id in verdicts and c not in accepted and c not in uncertain]
    unjudged = [c for c in judged_pool if c.candidate_id not in verdicts]
    ranked = rubric.rank_selected(accepted, verdicts) + rubric.rank_selected(uncertain, verdicts)
    uncertain_ids = {c.candidate_id for c in uncertain}
    finalized: dict[str, FinalizedCardView] = {}
    tried: list[CandidateView] = []
    queue = list(ranked)
    for _ in range(FINALIZE_ROUNDS):
        need = config.top_n - len(finalized)
        if need <= 0 or not queue or (config.llm_allowed is not None and not config.llm_allowed()):
            break
        batch, queue = queue[:need], queue[need:]
        tried.extend(batch)
        finalized.update(await _finalize(insight, query_text, batch, documents, verdicts, config))
    order = {c.candidate_id: i for i, c in enumerate(ranked)}
    shown = sorted((c for c in tried if c.candidate_id in finalized), key=lambda c: (
        c.candidate_id in uncertain_ids, -rubric.priority(verdicts[c.candidate_id]),
        -verdicts[c.candidate_id].confidence, order[c.candidate_id]))[: config.top_n]
    text_failed = [c for c in tried if c.candidate_id not in finalized]
    outcome.weak_signals_total = len(accepted)
    outcome.weak_signals_confident = sum(1 for c in accepted if verdicts[c.candidate_id].confidence >= 0.75)
    outcome.judge_rejected = len(rejected) + len(prefiltered)
    outcome.excluded_written = await results.add_excluded(job_id, _excluded_rows(
        rest, prefiltered, rejected, unjudged, queue, text_failed, verdicts))
    for position, candidate in enumerate(shown, 1):
        await check()
        item = _build_item(job_id, position, candidate, verdicts[candidate.candidate_id],
                           finalized[candidate.candidate_id], documents, compositions[candidate.candidate_id],
                           candidate.candidate_id in uncertain_ids)
        await results.add_item(item)
        outcome.items_written += 1
        outcome.narratives_generated += 1
    log.info("stage.narrate_rubric", pool=len(pool), prefiltered=len(prefiltered), judged=len(verdicts),
             accepted=len(accepted), uncertain=len(uncertain), tried=len(tried), finalized=len(finalized),
             shown=outcome.items_written, text_failed=len(text_failed))
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
    """Пакетная доводка; карточки, не прошедшие проверку, в ответ не входят."""
    if not shown or not config.finalize_enabled:
        return {}
    cards = [FinalizeRequestCard(c, _docs(c, documents)[:FINALIZE_SOURCES], verdicts[c.candidate_id].stage,
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
    text_failed: list[CandidateView],
    verdicts: dict[str, JudgeVerdictView],
) -> list[ExcludedCandidate]:
    """Исключённые кандидаты с причинами (ТЗ: причины исключения зрелых и нерелевантных кандидатов)."""
    rows: list[ExcludedCandidate] = []

    def add(candidate: CandidateView, decision: Decision, reason: str, text: str, score: float) -> None:
        rows.append(ExcludedCandidate(candidate.candidate_id, candidate.title_auto[:200], round(min(max(score, 0.0), 1.0), 4),
                                      decision, reason, text[:500], candidate.document_count))

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
            "Рубричная оценка не получена: модель не ответила разборчиво и при повторе по одному.", candidate.score)
    for candidate in overflow:
        verdict = verdicts[candidate.candidate_id]
        add(candidate, Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_BELOW_TOP_N",
            f"Кандидат вне ТОП-N по баллу стадия + тренд ({rubric.priority(verdict)} из 7). {verdict.reason_ru}",
            verdict.confidence)
    for candidate in text_failed:
        verdict = verdicts[candidate.candidate_id]
        add(candidate, Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_TEXT_FAILED",
            "Карточку не удалось полностью заполнить на русском по источникам — показан следующий кандидат.",
            verdict.confidence)
    return rows


def _build_item(
    job_id: str, position: int, candidate: CandidateView, verdict: JudgeVerdictView, card: FinalizedCardView,
    documents: dict[str, DocumentView], composition: rubric.Composition, uncertain: bool,
) -> ResultItem:
    """Элемент выдачи: тексты пакетной доводки (все поля на русском), рубрика — в объяснении и предикторах."""
    status = rubric.status_explanation(verdict, verdict.stage, verdict.trend)
    if uncertain:
        status = ("Требует экспертной проверки: рубрика не подтвердила все четыре условия слабого сигнала. "
                  + status)[:500]
    features = rubric.feature_rows(rubric.rubric_features(verdict, composition, verdict.stage, verdict.trend), candidate)
    companies = f" Компании и организации из источников: {', '.join(card.companies)}." if card.companies else ""
    explanation = f"{card.why_ru}{companies} {card.stage_reason_ru} {card.trend_reason_ru}".strip()
    shown_sources = {doc_id: documents[doc_id] for doc_id in card.source_summaries if doc_id in documents}
    return ResultItem(
        job_id=job_id, rank=position, candidate_id=candidate.candidate_id, title_ru=card.title_ru[:200],
        title_auto=candidate.title_auto[:200],
        score=round(min(verdict.confidence, 0.5) if uncertain else verdict.confidence, 4),
        decision_reason="RUBRIC_U" if uncertain else "RUBRIC_R", decision_explanation_ru=status,
        description_ru=card.description_ru, advantage_ru=card.advantage_ru, case_example_ru=card.case_example_ru,
        case_document_id=card.case_document_id, explanation_ru=explanation, narrative_status=NarrativeStatus.GENERATED,
        llm_provider=card.llm_provider, llm_model=card.llm_model, prompt_version=card.prompt_version,
        document_count=candidate.document_count, features=features,
        sources=build_sources(candidate, shown_sources, card.source_summaries),
        predicted_stage=verdict.stage, predicted_trend=verdict.trend,
    )
