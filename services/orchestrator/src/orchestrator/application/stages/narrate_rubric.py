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

import dataclasses
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

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
from ws_common import rubric_model, signal_classifier
from ws_common.logging import get_logger

DOCUMENTS_BATCH = 150
# Три раунда замены: отказ доводки заменяется следующим кандидатом R (стенд 29.09: 45 R → 24 карточки).
FINALIZE_ROUNDS = 3
FINALIZE_SOURCES = 5
# Рубрика — вызовами по 20 кандидатов (5 вызовов LLM по 4): пул 60–100 кандидатов одним вызовом RPC — это 15–25
# вызовов LLM, на GigaChat-2-Max он не укладывается в срок RPC 660 с, а обрыв по сроку увёл бы задание в прежнюю ветку.
RUBRIC_CHUNK = 20
LIKELY = 0.5
CONFIDENT = 0.75
log = get_logger("orchestrator.stage.narrate_rubric")


def profile_text(verdict: JudgeVerdictView, candidate: CandidateView) -> str:
    """Профиль для классификатора: извлечённое название технологии и её профиль по источникам."""
    return f"{verdict.technology_ru or candidate.title_auto}. {verdict.profile_ru or verdict.reason_ru}"


def probability(model: dict | None, verdict: JudgeVerdictView, candidate: CandidateView,
                documents: tuple[DocumentView, ...], classifier: dict | None = None) -> tuple[float, dict[str, float]]:
    """Вероятность слабого сигнала: локальный классификатор по профилю (этап 1 ТЗ); без него — модель v3
    на признаках рубрики и источников; без обеих — порядок рубрики (R > U > отказ)."""
    if classifier is not None:
        text = profile_text(verdict, candidate)
        return round(signal_classifier.predict(classifier, text), 4), {"__profile__": text}
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
        await _save_trace(results, job_id, {"path": "legacy", "config": config_dict(config),
                                            "reason": "insight_disabled" if insight is None else "deadline"})
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    model = rubric_model.load(config.rubric_model_path) if config.rubric_model_path else None
    classifier = signal_classifier.load(config.profile_model_path) if config.profile_model_path else None
    if classifier is None and config.signal_model_path:
        classifier = signal_classifier.load(config.signal_model_path)
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
    raw = await _judge(insight, query_text, judged_pool, documents, compositions, config.llm_allowed)
    if not raw:
        log.warning("stage.narrate_rubric.judge_unavailable", pool=len(judged_pool))
        await _save_trace(results, job_id, {
            "path": "legacy", "reason": "rubric_unavailable", "config": config_dict(config),
            "candidates": {"weak": [c.candidate_id for c in weak], "excluded": [c.candidate_id for c in excluded]},
            "pool": [c.candidate_id for c in pool], "rest": [_reason_row(c) for c in rest],
            "prefiltered": [[c.candidate_id, d.value, reason] for c, (d, reason, _) in prefiltered]})
        return await run_narrate(analyzer=analyzer, collector=collector, insight=insight, results=results,
                                 job_id=job_id, query_text=query_text, analysis_id=analysis_id, config=legacy,
                                 check=check)
    stage_map = rubric.parse_calibration(config.stage_calibration, 1, 4)
    trend_map = rubric.parse_calibration(config.trend_calibration, 1, 3)
    verdicts = {cid: replace(v, stage=stage_map.get(v.stage, v.stage), trend=trend_map.get(v.trend, v.trend))
                for cid, v in raw.items()}
    judged = [c for c in judged_pool if c.candidate_id in verdicts]
    unjudged = [c for c in judged_pool if c.candidate_id not in verdicts]
    scored = {c.candidate_id: probability(model, verdicts[c.candidate_id], c, _docs(c, documents), classifier)
              for c in judged}
    order = {c.candidate_id: i for i, c in enumerate(judged)}
    ranked = sorted(judged, key=lambda c: (-scored[c.candidate_id][0], -rubric.priority(verdicts[c.candidate_id]),
                                           order[c.candidate_id]))
    # Решение о показе — рубрика (R), порядок внутри R — локальная модель. 27.09 порог модели скрыл 15 кандидатов R
    # и показал общие. Если R меньше минимума — добираются лучшие по модели кандидаты U, затем прочие (с пометкой).
    accepted = [c for c in ranked if verdicts[c.candidate_id].code == "R"]
    # Добор — только из «данных недостаточно» (U): отвергнутые рубрикой (не по теме, общее, обзор, зрелое)
    # не показываются никогда (стенд 29.09: «серия книг IEEE для подростков» в теме инфраструктуры ИИ).
    rest_ranked = sorted((c for c in ranked if verdicts[c.candidate_id].code == "U"),
                         key=lambda c: -scored[c.candidate_id][0])
    eligible = accepted + rest_ranked[: max(0, config.rubric_min_cards - len(accepted))]
    below = [c for c in ranked if c not in eligible]
    ranked = eligible
    finalized: dict[str, FinalizedCardView] = {}
    final_dropped: dict[str, JudgeVerdictView] = {}
    review_dropped: dict[str, tuple[Decision, str, str]] = {}
    tried: list[CandidateView] = []
    queue = list(ranked)
    rounds: list[dict[str, object]] = []
    deadline_cut = False
    for _ in range(FINALIZE_ROUNDS):
        need = config.top_n - len(finalized)
        if need <= 0 or not queue:
            break
        if config.llm_allowed is not None and not config.llm_allowed():
            deadline_cut = True  # повтор режима А с этим заданием не сравним: доводку остановил срок
            break
        batch, queue = queue[:need], queue[need:]
        tried.extend(batch)
        produced = await _finalize(insight, query_text, batch, documents, verdicts, config)
        finalized.update(produced)
        # Правило обзоров — и по источникам готовой карточки: доводка оставляет источники, которые описывают
        # технологию, и в выдаче v12 ими оказывались обзоры («AI-Driven Predictive Maintenance» — три обзора).
        reviews = _review_dominated(batch, finalized, documents)
        review_dropped.update(reviews)
        for candidate_id in review_dropped:
            finalized.pop(candidate_id, None)
        checked: dict[str, JudgeVerdictView] = {}
        if config.final_check_enabled:
            checked = await _final_check(insight, query_text, batch, finalized, documents)
            final_dropped.update(checked)
            for candidate_id in final_dropped:
                finalized.pop(candidate_id, None)
        rounds.append({"batch": [c.candidate_id for c in batch], "finalized": sorted(produced),
                       "review_dropped": sorted(reviews), "final_dropped": sorted(checked)})
    rejected = final_dropped.keys() | review_dropped.keys()
    rank_of = {c.candidate_id: i for i, c in enumerate(ranked)}
    shown = sorted((c for c in tried if c.candidate_id in finalized), key=lambda c: rank_of[c.candidate_id])
    text_failed = [c for c in tried if c.candidate_id not in finalized and c.candidate_id not in rejected]
    dropped_final = [c for c in tried if c.candidate_id in final_dropped]
    dropped_reviews = [(c, review_dropped[c.candidate_id]) for c in tried if c.candidate_id in review_dropped]
    # Резервная ветка берёт только не отклонённых финальной проверкой и правилом обзоров: решение о недопустимости
    # технологии не отменяется тем, что описание не удалось довести (аудит 29.09: shown=3 при final_rejections=3).
    allowed = [c for c in ranked if c.candidate_id not in rejected]
    fallback = not shown and bool(allowed)
    if fallback:  # пустой выдачи при допустимых кандидатах не бывает: прежний генератор карточек
        shown, text_failed = allowed[: config.top_n], []
        queue = allowed[config.top_n :]
    outcome.weak_signals_total = sum(1 for c in judged if verdicts[c.candidate_id].code == "R")
    # «Уверены более чем на 75 %» — по той же уверенности, что показана в карточках (с калибровкой, если включена).
    # Считаются только показанные карточки: счётчик стоит рядом со списком «Найденные слабые сигналы», и число
    # должно совпадать с тем, что видно в карточках (стенд 29.09: «1 из 9» при четырёх карточках 67–71 %).
    outcome.weak_signals_confident = sum(
        1 for c in shown if verdicts[c.candidate_id].code == "R"
        and display_score(scored[c.candidate_id][0], "R", config.confidence_calibration) >= CONFIDENT)
    outcome.judge_rejected = len(prefiltered)
    outcome.excluded_written = await results.add_excluded(job_id, _excluded_rows(
        rest, prefiltered + dropped_reviews, unjudged, queue + below, text_failed, verdicts, scored, dropped_final,
        final_dropped))
    await _save_trace(results, job_id, {
        "path": "rubric",
        "config": config_dict(config),
        "models": {"classifier": "profile" if config.profile_model_path and classifier is not None
                   else "signal" if classifier is not None else "rubric_model" if model is not None else "rubric_order",
                   "sha256": {name: file_digest(path) for name, path in (
                       ("signal_model_path", config.signal_model_path), ("profile_model_path", config.profile_model_path),
                       ("rubric_model_path", config.rubric_model_path)) if path}},
        "candidates": {"weak": [c.candidate_id for c in weak], "excluded": [c.candidate_id for c in excluded]},
        "pool": [c.candidate_id for c in pool],
        "rest": [_reason_row(c) for c in rest],
        "prefiltered": [[c.candidate_id, d.value, reason] for c, (d, reason, _) in prefiltered],
        "judged": {cid: verdict_dict(v) for cid, v in verdicts.items()},
        "raw_stage_trend": {cid: [v.stage, v.trend] for cid, v in raw.items()},
        "unjudged": [c.candidate_id for c in unjudged],
        "probabilities": {cid: value[0] for cid, value in scored.items()},
        "accepted": [c.candidate_id for c in accepted],
        "uncertain_fill": [c.candidate_id for c in ranked if verdicts[c.candidate_id].code != "R"],
        "below": [c.candidate_id for c in below],
        "rounds": rounds,
        "deadline_cut": deadline_cut,
        "not_tried": [c.candidate_id for c in queue],
        "text_failed": [c.candidate_id for c in text_failed],
        "review_dropped": {cid: [decision.value, reason] for cid, (decision, reason, _) in review_dropped.items()},
        "final_dropped": {cid: {"code": v.code, "reason_ru": v.reason_ru} for cid, v in final_dropped.items()},
        "fallback": fallback,
        "shown": [[position, c.candidate_id, display_score(scored[c.candidate_id][0], verdicts[c.candidate_id].code,
                                                           config.confidence_calibration),
                   verdicts[c.candidate_id].code, c.candidate_id in finalized] for position, c in enumerate(shown, 1)],
    })
    for position, candidate in enumerate(shown, 1):
        await check()
        item = await _build_item(insight, job_id, query_text, position, candidate, verdicts[candidate.candidate_id],
                                 finalized.get(candidate.candidate_id), documents, scored[candidate.candidate_id],
                                 classifier or model, config, outcome)
        await results.add_item(item)
        outcome.items_written += 1
        if item.narrative_status is NarrativeStatus.GENERATED:
            outcome.narratives_generated += 1
        else:
            outcome.narratives_fallback += 1
    log.info("stage.narrate_rubric", pool=len(pool), prefiltered=len(prefiltered), judged=len(judged),
             likely=outcome.weak_signals_total, tried=len(tried), finalized=len(finalized),
             review_dropped=len(review_dropped), shown=outcome.items_written, fallback=fallback, model=bool(model),
             classifier=bool(classifier))
    return outcome


def config_dict(config: NarrateConfig) -> dict[str, object]:
    """Настройки стадии для трассы: все поля, кроме функции проверки срока."""
    return {f.name: getattr(config, f.name) for f in dataclasses.fields(config) if f.name != "llm_allowed"}


def verdict_dict(verdict: JudgeVerdictView) -> dict[str, object]:
    """Вердикт рубрики для трассы (стадия и тренд — после калибровки)."""
    return {"code": verdict.code, "stage": verdict.stage, "trend": verdict.trend, "confidence": verdict.confidence,
            "on_topic": verdict.on_topic, "concrete": verdict.concrete, "early_stage": verdict.early_stage,
            "verifiable": verdict.verifiable, "technology_ru": verdict.technology_ru, "profile_ru": verdict.profile_ru,
            "reason_ru": verdict.reason_ru}


def file_digest(path: str) -> str:
    """SHA-256 файла модели (пусто, если файла нет): версия модели в трассе задания."""
    file = Path(path)
    return hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else ""


def _reason_row(candidate: CandidateView) -> list[object]:
    return [candidate.candidate_id, candidate.decision.value, candidate.decision_reason]


async def _save_trace(results: ResultRepository, job_id: str, payload: dict[str, object]) -> None:
    """Трасса стадии; сбой записи не обрывает выдачу, но виден в журнале (выгрузка отметит её отсутствие)."""
    try:
        await results.save_trace(job_id, "narrate", payload)
    except Exception as error:  # noqa: BLE001 - потеря трассы не должна стоить пользователю выдачи
        log.error("stage.narrate_rubric.trace_failed", job_id=job_id, error=str(error)[:300])


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
    compositions: dict[str, rubric.Composition], llm_allowed: Callable[[], bool] | None = None,
) -> dict[str, JudgeVerdictView]:
    """Рубричная оценка пула частями по RUBRIC_CHUNK; недоступность — пустой словарь.

    Отказ части не срывает остальные: её кандидаты остаются без вердикта. Перед каждой следующей частью
    проверяется срок задания: если он не позволяет ещё один вызов LLM, оставшиеся кандидаты не оцениваются.
    """
    verdicts: dict[str, JudgeVerdictView] = {}
    for start in range(0, len(pool), RUBRIC_CHUNK):
        if start and llm_allowed is not None and not llm_allowed():
            log.warning("stage.narrate_rubric.judge_deadline", judged=len(verdicts), left=len(pool) - start)
            break
        items = [RubricRequestItem(c, _docs(c, documents), rubric.composition_ru(compositions[c.candidate_id]))
                 for c in pool[start : start + RUBRIC_CHUNK]]
        try:
            verdicts.update(await insight.judge_rubric(query_text, items))
        except (UpstreamUnavailable, StageFailed) as error:
            log.warning("stage.narrate_rubric.judge_failed", start=start, size=len(items), error=str(error)[:200])
    return verdicts


async def _finalize(
    insight: InsightClient, query_text: str, shown: list[CandidateView], documents: dict[str, DocumentView],
    verdicts: dict[str, JudgeVerdictView], config: NarrateConfig,
) -> dict[str, FinalizedCardView]:
    """Пакетная доводка; карточки, не прошедшие проверку, в ответ не входят."""
    if not shown or not config.finalize_enabled:
        return {}
    cards = [FinalizeRequestCard(c, _docs(c, documents)[:FINALIZE_SOURCES], verdicts[c.candidate_id].stage,
                                 verdicts[c.candidate_id].trend,
                                 f"{verdicts[c.candidate_id].technology_ru}: {verdicts[c.candidate_id].reason_ru}".strip(": "))
             for c in shown]
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
    dropped_final: list[CandidateView] | None = None,
    final_dropped: dict[str, JudgeVerdictView] | None = None,
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
    for candidate in dropped_final or []:
        verdict = (final_dropped or {})[candidate.candidate_id]
        add(candidate, rubric.CODE_DECISION.get(verdict.code, Decision.INSUFFICIENT_EVIDENCE), f"RUBRIC_FINAL_{verdict.code}",
            f"Финальная проверка готовой карточки: {rubric.CODE_RU.get(verdict.code, verdict.code)}. {verdict.reason_ru}",
            verdict.confidence)
    return rows


FINAL_DROP = frozenset({"N-GEN", "N-MAT", "N-OVR", "N-OFF", "N-NOI", "N-HYP", "N-FUND"})


async def _final_check(
    insight: InsightClient, query_text: str, batch: list[CandidateView], finalized: dict[str, FinalizedCardView],
    documents: dict[str, DocumentView],
) -> dict[str, JudgeVerdictView]:
    """Рубрика по готовой карточке: название и описание — конкретная ранняя технология по теме? Общее, зрелое,
    обзор и не по теме возвращаются для замены следующим кандидатом. Сбой проверки карточки не отбрасывает."""
    items = []
    for candidate in batch:
        card = finalized.get(candidate.candidate_id)
        if card is None:
            continue
        docs = tuple(documents[d] for d in card.source_summaries if d in documents)
        items.append(RubricRequestItem(replace(candidate, title_auto=card.title_ru[:200], keyphrases=()), docs,
                                       f"Итоговая карточка. Название: {card.title_ru}. Описание: {card.description_ru}"))
    if not items:
        return {}
    try:
        verdicts = await insight.judge_rubric(query_text, items, final_check=True)
    except (UpstreamUnavailable, StageFailed) as error:
        log.warning("stage.narrate_rubric.final_check_failed", error=str(error)[:200])
        return {}
    return {cid: verdict for cid, verdict in verdicts.items() if verdict.code in FINAL_DROP}


def _review_dominated(
    batch: list[CandidateView], finalized: dict[str, FinalizedCardView], documents: dict[str, DocumentView],
) -> dict[str, tuple[Decision, str, str]]:
    """Готовые карточки пачки, у которых обзоры — не меньше половины показанных источников: не показываются."""
    dropped: dict[str, tuple[Decision, str, str]] = {}
    for candidate in batch:
        card = finalized.get(candidate.candidate_id)
        if card is None:
            continue
        shown = [documents[doc_id] for doc_id in card.source_summaries if doc_id in documents]
        verdict = rubric.reviews_dominate(rubric.composition_of(shown), "источников карточки")
        if verdict is not None:
            dropped[candidate.candidate_id] = verdict
    return dropped


def display_score(prob: float, code: str, calibrate: bool) -> float:
    """Отображаемая уверенность: принятый рубрикой сигнал — 0,5 + 0,5 × вероятность модели (порядок сохраняется)."""
    if not calibrate:
        return round(prob, 2)
    # Две цифры: интерфейс показывает целые проценты, полоса High и счётчик «> 75 %» считают по тому же значению
    # (раньше 0,7496 показывалось как «75 %», но было Medium и не входило в счётчик).
    return round(0.5 + 0.5 * prob, 2) if code == "R" else round(0.5 * prob, 2)


def model_features(model: dict | None, values: dict[str, float], candidate: CandidateView) -> tuple[FeatureRow, ...]:
    """Ключевые предикторы: вклады признаков локальной модели по модулю, затем признаки analyzer."""
    rows: list[FeatureRow] = []
    if model is not None and "__profile__" in values:
        for token, contribution in signal_classifier.contributions(model, values["__profile__"]):
            direction = (FeatureDirection.SUPPORTS_WEAK_SIGNAL if contribution > 0 else FeatureDirection.SUPPORTS_MATURE)
            rows.append(FeatureRow(f"sc_{token}"[:64], f"Признак профиля «{token.replace('_', ' ')}»", 1.0,
                                   round(float(contribution), 4), direction, len(rows) + 1))
    elif model is not None:
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
    status = (f"Вероятность слабого сигнала по локальному классификатору: {prob:.0%}"
              + (". " if verdict.code == "R" else " — кандидат не признан слабым сигналом рубрикой, требует экспертной проверки. ")
              + rubric.status_explanation(verdict, verdict.stage, verdict.trend))[:500]
    features = model_features(model, values, candidate)
    common = dict(job_id=job_id, rank=position, candidate_id=candidate.candidate_id,
                  title_auto=candidate.title_auto[:200], score=display_score(prob, verdict.code, config.confidence_calibration),
                  decision_reason="RUBRIC_MODEL",
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
