"""Обработка запросов JudgeCandidates и FinalizeCards без транспорта gRPC.

Те же функции вызывает gRPC-сервер и повтор режима А (аудит A3): разбор запроса, вызов сценария и сборка ответа
не дублируются, поэтому промпты при повторе совпадают с промптами живого задания.
"""

from __future__ import annotations

from weaksignals.insight.v1 import insight_pb2

from insight.application.use_cases.finalize_cards import CardInput, FinalizeCards
from insight.application.use_cases.judge_candidates import JudgeCandidates, JudgeItem
from insight.application.use_cases.rubric_judge import RubricItem, RubricJudge, RubricSource

# Режимы JudgeCandidates: рубричная оценка и финальная проверка готовых карточек (тот же ответ, строже тема).
RUBRIC_MODE = "rubric_v2"
FINAL_CHECK_MODE = "rubric_final"


async def judge(
    request: insight_pb2.JudgeCandidatesRequest,
    judge_candidates: JudgeCandidates | None,
    rubric_judge: RubricJudge | None,
) -> insight_pb2.JudgeCandidatesResponse:
    """Смысловая оценка кандидатов; недоступность модели — пустой ответ с used_fallback, не ошибка."""
    if request.mode in (RUBRIC_MODE, FINAL_CHECK_MODE):
        return await judge_rubric(request, rubric_judge)
    if judge_candidates is None or not request.items or not request.query_text.strip():
        return insight_pb2.JudgeCandidatesResponse(used_fallback=True)
    items = [
        JudgeItem(item.candidate_id, item.title, tuple(item.keyphrases), tuple(item.evidence))
        for item in request.items
    ]
    outcome = await judge_candidates.execute(request.query_text.strip()[:500], items)
    return insight_pb2.JudgeCandidatesResponse(
        verdicts=[
            insight_pb2.JudgeVerdict(
                candidate_id=v.candidate_id, verdict=v.verdict, relevance=v.relevance, reason_ru=v.reason_ru
            )
            for v in outcome.verdicts
        ],
        provider=outcome.provider,
        model=outcome.model,
        used_fallback=outcome.used_fallback,
    )


async def judge_rubric(
    request: insight_pb2.JudgeCandidatesRequest, rubric_judge: RubricJudge | None
) -> insight_pb2.JudgeCandidatesResponse:
    """Рубричная оценка (режим rubric_v2 или rubric_final): код рубрики, критерии, стадия, тренд, уверенность."""
    if rubric_judge is None or not request.items or not request.query_text.strip():
        return insight_pb2.JudgeCandidatesResponse(used_fallback=True)
    items = [
        RubricItem(item.candidate_id, item.title, tuple(item.keyphrases),
                   tuple(rubric_source(source) for source in item.sources), item.composition_ru)
        for item in request.items
    ]
    outcome = await rubric_judge.execute(request.query_text.strip()[:500], items,
                                         final_check=request.mode == FINAL_CHECK_MODE)
    return insight_pb2.JudgeCandidatesResponse(
        verdicts=[
            insight_pb2.JudgeVerdict(
                candidate_id=v.candidate_id, verdict=v.verdict, relevance=v.relevance, reason_ru=v.reason_ru,
                code=v.code, on_topic=v.on_topic, concrete=v.concrete, early_stage=v.early_stage,
                verifiable=v.verifiable, stage=v.stage, trend=v.trend, confidence=v.confidence,
                technology_ru=v.technology_ru, profile_ru=v.profile_ru,
            )
            for v in outcome.verdicts
        ],
        provider=outcome.provider,
        model=outcome.model,
        used_fallback=outcome.used_fallback,
    )


async def finalize(
    request: insight_pb2.FinalizeCardsRequest, finalize_cards: FinalizeCards | None
) -> insight_pb2.FinalizeCardsResponse:
    """Пакетная доводка карточек; недоступность модели — пустой ответ с used_fallback, не ошибка."""
    if finalize_cards is None or not request.cards or not request.query_text.strip():
        return insight_pb2.FinalizeCardsResponse(used_fallback=True)
    cards = [
        CardInput(card.candidate_id, card.title_auto, tuple(card.keyphrases),
                  tuple(rubric_source(source) for source in card.sources), card.stage, card.trend, card.judge_reason_ru)
        for card in request.cards
    ]
    outcome = await finalize_cards.execute(request.query_text.strip()[:500], cards)
    return insight_pb2.FinalizeCardsResponse(
        cards=[
            insight_pb2.FinalizedCard(
                candidate_id=card.candidate_id, title_ru=card.title_ru, description_ru=card.description_ru,
                advantage_ru=card.advantage_ru, case_example_ru=card.case_example_ru,
                case_document_id=card.case_document_id, why_ru=card.why_ru, companies=list(card.companies),
                stage=card.stage, trend=card.trend, stage_reason_ru=card.stage_reason_ru,
                trend_reason_ru=card.trend_reason_ru,
                source_summaries=[
                    insight_pb2.SourceSummary(document_id=document_id, summary_ru=summary,
                                              kind=insight_pb2.SummaryKind.Value(f"SUMMARY_KIND_{kind}"))
                    for document_id, summary, kind in card.source_summaries
                ],
            )
            for card in outcome.cards
        ],
        provider=outcome.provider,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
        used_fallback=outcome.used_fallback,
    )


def rubric_source(source) -> RubricSource:  # noqa: ANN001 - insight_pb2.JudgeSource
    """Источник из контракта → источник рубричной оценки."""
    return RubricSource(
        document_id=source.document_id, title=source.title, source_key=source.source_key,
        source_type=source.source_type, trust_level=source.trust_level, published=source.published,
        language_code=source.language_code, snippet=source.snippet, domain=source.domain,
    )
