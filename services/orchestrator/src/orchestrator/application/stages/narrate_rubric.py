"""Стадия 4 в режиме отбора rubric (v3): пул → предфильтр ТЗ → рубрика LLM (признаки) → локальная модель
(вероятность слабого сигнала) → ТОП-N по вероятности → пакетная доводка на русском → запись.

Решение и уверенность даёт локальная модель (ws_common.rubric_model, логистическая регрессия на признаках рубрики,
состава источников и analyzer): на проверке по темам B v2 она ранжирует лучше рубрики LLM (PR-AUC 0,535 против
0,454, точность в ТОП-3 темы 0,414 против 0,391), а самооценка уверенности LLM в ней имеет отрицательный вес —
поэтому в v2 все карточки показывали 100 %. Кандидаты не отсекаются по коду рубрики, а ранжируются: ТОП-N
заполняется всегда, пока есть кандидаты; карточки с вероятностью ниже 50 % помечены «требует проверки».
Если пакетная доводка не приняла ни одной карточки, карточки строятся прежним генератором — пустой выдачи
при найденных кандидатах не бывает.
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
from orchestrator.domain.entities import ExcludedCandidate, FeatureRow, ResultItem
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import Decision, FeatureDirection, NarrativeStatus
from ws_common import rubric_model
from ws_common.logging import get_logger

DOCUMENTS_BATCH = 150
FINALIZE_ROUNDS = 2
FINALIZE_SOURCES = 5
LIKELY = 0.5
CONFIDENT = 0.75
log = get_logger("orchestrator.stage.narrate_rubric")


def probability(model: dict | None, verdict: JudgeVerdictView, candidate: CandidateView,
                documents: tuple[DocumentView, ...]) -> tuple[float, dict[str, float]]:
    """Вероятность слабого сигнала по локальной модели; без файла модели — порядок рубрики (R > U > отказ)."""
    values = rubric_model.features(
        verdict.code, verdict.confidence, verdict.stage, verdict.trend, [d.source_type for d in documents],
        [str(getattr(d.trust_level, "value", d.trust_level)) for d in documents], [d.title or "" for d in documents],
        [d.published_at.year if d.published_at else None for d in documents], candidate.score)
    if model is None:
        base = {"R": 0.7, "U": 0.45}.get(verdict.code, 0.2)
        return round(base + 0.05 * verdict.confidence, 4), values
    return round(rubric_model.predict(model, values), 4), values


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
    """ТОП-N по вероятности локальной модели; карточки — пакетной доводкой на русском."""
    legacy = replace(config, selection_mode="legacy")
    llm_ok = insight is not None and (config.llm_allowed is None or config.llm_allowed())
    if not llm_ok:
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    model = rubric_model.load(config.rubric_model_path) if config.rubric_model_path else None
    weak, excluded = await collect_candidates(analyzer, analysis_id, check)
    outcome = NarrateOutcome(candidates_found=len(weak) + len(excluded))
    pool, rest = rubric.pool_candidates(weak, excluded, config.rubric_pool)
    documents = await _load_all(collector, pool, check)
    compositions = {c.candidate_id: rubric.composition_of(_docs(c, documents)) for c in pool}
    prefiltered: list[tuple[CandidateView, tuple[Decision, str, str]]] = []
    judged_pool: list[CandidateView] = []
    for candidate in pool:
        verdict = rubric.prefilter(compositions[candidate.candidate_id], check_reviews=False)
        (prefiltered.append((candidate, verdict)) if verdict else judged_pool.append(candidate))
    raw = await _judge(insight, query_text, judged_pool, documents, compositions)
    if not raw:
        log.warning("stage.narrate_rubric.judge_unavailable", pool=len(judged_pool))
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    stage_map = rubric.parse_calibration(config.stage_calibration, 1, 4)
    trend_map = rubric.parse_calibration(config.trend_calibration, 1, 3)
    verdicts = {cid: replace(v, stage=stage_map.get(v.stage, v.stage), trend=trend_map.get(v.trend, v.trend))
                for cid, v in raw.items()}
    judged = [c for c in judged_pool if c.candidate_id in verdicts]
    unjudged = [c for c in judged_pool if c.candidate_id not in verdicts]
    scored = {c.candidate_id: probability(model, verdicts[c.candidate_id], c, _docs(c, documents)) for c in judged}
    order = {c.candidate_id: i for i, c in enumerate(judged)}
    ranked = sorted(judged, key=lambda c: (-scored[c.candidate_id][0], -rubric.priority(verdicts[c.candidate_id]),
                                           order[c.candidate_id]))
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
    rank_of = {c.candidate_id: i for i, c in enumerate(ranked)}
    shown = sorted((c for c in tried if c.candidate_id in finalized), key=lambda c: rank_of[c.candidate_id])
    text_failed = [c for c in tried if c.candidate_id not in finalized]
    fallback = not shown and bool(ranked)
    if fallback:  # пустой выдачи при найденных кандидатах не бывает: прежний генератор карточек
        shown, text_failed = ranked[: config.top_n], []
        queue = ranked[config.top_n :]
    outcome.weak_signals_total = sum(1 for c in judged if scored[c.candidate_id][0] >= LIKELY)
    outcome.weak_signals_confident = sum(1 for c in judged if scored[c.candidate_id][0] >= CONFIDENT)
    outcome.judge_rejected = len(prefiltered)
    outcome.excluded_written = await results.add_excluded(job_id, _excluded_rows(
        rest, prefiltered, unjudged, queue, text_failed, verdicts, scored))
    for position, candidate in enumerate(shown, 1):
        await check()
        item = await _build_item(insight, job_id, query_text, position, candidate, verdicts[candidate.candidate_id],
                                 finalized.get(candidate.candidate_id), documents, scored[candidate.candidate_id],
                                 model, config, outcome)
        await results.add_item(item)
        outcome.items_written += 1
        if item.narrative_status is NarrativeStatus.GENERATED:
            outcome.narratives_generated += 1
        else:
            outcome.narratives_fallback += 1
    log.info("stage.narrate_rubric", pool=len(pool), prefiltered=len(prefiltered), judged=len(judged),
             likely=outcome.weak_signals_total, tried=len(tried), finalized=len(finalized),
             shown=outcome.items_written, fallback=fallback, model=bool(model))
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
    unjudged: list[CandidateView],
    overflow: list[CandidateView],
    text_failed: list[CandidateView],
    verdicts: dict[str, JudgeVerdictView],
    scored: dict[str, tuple[float, dict[str, float]]],
) -> list[ExcludedCandidate]:
    """Исключённые кандидаты с причинами (ТЗ: причины исключения зрелых и нерелевантных кандидатов)."""
    rows: list[ExcludedCandidate] = []

    def add(candidate: CandidateView, decision: Decision, reason: str, text: str, score: float) -> None:
        rows.append(ExcludedCandidate(candidate.candidate_id, candidate.title_auto[:200],
                                      round(min(max(score, 0.0), 1.0), 4), decision, reason, text[:500],
                                      candidate.document_count))

    for candidate in rest:
        decision = candidate.decision if candidate.decision is not Decision.WEAK_SIGNAL else Decision.INSUFFICIENT_EVIDENCE
        add(candidate, decision, candidate.decision_reason or "RUBRIC_POOL_LIMIT",
            candidate.decision_explanation_ru or "Не вошёл в пул рубричной оценки.", candidate.score)
    for candidate, (decision, reason, text) in prefiltered:
        add(candidate, decision, reason, text, candidate.score)
    for candidate in unjudged:
        add(candidate, Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_UNJUDGED",
            "Рубричная оценка не получена: модель не ответила разборчиво и при повторе по одному.", candidate.score)
    for candidate in overflow + text_failed:
        verdict = verdicts[candidate.candidate_id]
        prob = scored[candidate.candidate_id][0]
        failed = candidate in text_failed
        add(candidate, rubric.CODE_DECISION.get(verdict.code, Decision.INSUFFICIENT_EVIDENCE),
            "RUBRIC_TEXT_FAILED" if failed else f"RUBRIC_BELOW_TOP_N_{verdict.code}",
            ("Карточку не удалось заполнить на русском по источникам — показан следующий кандидат. " if failed else
             f"Вне ТОП-N: вероятность локальной модели {prob:.0%}; рубрика — "
             f"{rubric.CODE_RU.get(verdict.code, verdict.code)}. ") + verdict.reason_ru, prob)
    return rows


def model_features(model: dict | None, values: dict[str, float], candidate: CandidateView) -> tuple[FeatureRow, ...]:
    """Ключевые предикторы: вклады признаков локальной модели по модулю, затем признаки analyzer."""
    rows: list[FeatureRow] = []
    if model is not None:
        for name, value, contribution in rubric_model.contributions(model, values):
            direction = (FeatureDirection.SUPPORTS_WEAK_SIGNAL if contribution > 0.02 else
                         FeatureDirection.SUPPORTS_MATURE if contribution < -0.02 else FeatureDirection.NEUTRAL)
            rows.append(FeatureRow(f"lm_{name}", rubric_model.LABELS_RU.get(name, name), round(float(value), 4),
                                   round(float(contribution), 4), direction, len(rows) + 1))
    seen = {row.feature_name for row in rows}
    for feature in candidate.features:
        if feature.feature_name not in seen:
            rows.append(FeatureRow(feature.feature_name, feature.label_ru, feature.value, feature.contribution,
                                   feature.direction, len(rows) + 1))
    return tuple(rows)


async def _build_item(
    insight: InsightClient | None, job_id: str, query_text: str, position: int, candidate: CandidateView,
    verdict: JudgeVerdictView, card: FinalizedCardView | None, documents: dict[str, DocumentView],
    scored: tuple[float, dict[str, float]], model: dict | None, config: NarrateConfig, outcome: NarrateOutcome,
) -> ResultItem:
    """Элемент выдачи: уверенность и предикторы — локальная модель, тексты — пакетная доводка на русском."""
    prob, values = scored
    status = (f"Вероятность слабого сигнала по локальной модели: {prob:.0%}"
              + (" — кандидат, требует экспертной проверки. " if prob < LIKELY else ". ")
              + rubric.status_explanation(verdict, verdict.stage, verdict.trend))[:500]
    features = model_features(model, values, candidate)
    common = dict(job_id=job_id, rank=position, candidate_id=candidate.candidate_id,
                  title_auto=candidate.title_auto[:200], score=prob, decision_reason="RUBRIC_MODEL",
                  decision_explanation_ru=status, document_count=candidate.document_count, features=features,
                  predicted_stage=verdict.stage, predicted_trend=verdict.trend)
    if card is not None:
        companies = f" Компании и организации из источников: {', '.join(card.companies)}." if card.companies else ""
        shown_sources = {doc_id: documents[doc_id] for doc_id in card.source_summaries if doc_id in documents}
        return ResultItem(
            **common, title_ru=card.title_ru[:200], description_ru=card.description_ru, advantage_ru=card.advantage_ru,
            case_example_ru=card.case_example_ru, case_document_id=card.case_document_id,
            explanation_ru=f"{card.why_ru}{companies} {card.stage_reason_ru} {card.trend_reason_ru}".strip(),
            narrative_status=NarrativeStatus.GENERATED, llm_provider=card.llm_provider, llm_model=card.llm_model,
            prompt_version=card.prompt_version, sources=build_sources(candidate, shown_sources, card.source_summaries))
    use_llm = config.llm_allowed is None or config.llm_allowed()
    if not use_llm:
        outcome.deadline_fallbacks += 1
    docs = {d.document_id: d for d in _docs(candidate, documents)}
    narrative = await _narrate_candidate(insight if use_llm else None, job_id, query_text, candidate, docs, config)
    return ResultItem(
        **common, title_ru=(narrative.title_ru or candidate.title_auto)[:200], description_ru=narrative.description_ru,
        advantage_ru=narrative.advantage_ru, case_example_ru=narrative.case_example_ru,
        case_document_id=narrative.case_document_id, explanation_ru=narrative.explanation_ru,
        narrative_status=NarrativeStatus(narrative.status), llm_provider=narrative.llm_provider,
        llm_model=narrative.llm_model, prompt_version=narrative.prompt_version,
        sources=build_sources(candidate, docs, narrative.source_summaries))
