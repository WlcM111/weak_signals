"""gRPC-сервер `InsightService` (асинхронный): валидация, вызов сценариев, коды ошибок."""

from __future__ import annotations

import asyncio

import grpc
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

from insight.adapters.inbound import mappers
from insight.application.use_cases.expand_query import ExpandQuery
from insight.application.use_cases.generate_insight import GenerateInsight
from insight.application.use_cases.finalize_cards import CardInput, FinalizeCards
from insight.application.use_cases.judge_candidates import JudgeCandidates, JudgeItem
from insight.application.use_cases.rubric_judge import RubricItem, RubricJudge, RubricSource
from insight.application.use_cases.get_provider_status import GetProviderStatus
from insight.application.validation import validate_query_text
from insight.domain.errors import (
    AppError,
    ResourceExhaustedError,
    UnavailableError,
    ValidationError,
)
from ws_common.logging import get_logger

_STATUS_BY_ERROR: tuple[tuple[type[AppError], grpc.StatusCode], ...] = (
    (ValidationError, grpc.StatusCode.INVALID_ARGUMENT),
    (ResourceExhaustedError, grpc.StatusCode.RESOURCE_EXHAUSTED),
    (UnavailableError, grpc.StatusCode.UNAVAILABLE),
)


class InsightServicer(insight_pb2_grpc.InsightServiceServicer):
    """Транспортный слой: перевод proto ↔ домен и прикладных ошибок в статусы gRPC."""

    def __init__(
        self,
        expand_query: ExpandQuery,
        generate_insight: GenerateInsight,
        provider_status: GetProviderStatus,
        max_queue: int = 100,
        judge_candidates: JudgeCandidates | None = None,
        rubric_judge: RubricJudge | None = None,
        finalize_cards: FinalizeCards | None = None,
    ) -> None:
        self._judge_candidates = judge_candidates
        self._rubric_judge = rubric_judge
        self._finalize_cards = finalize_cards
        self._expand_query = expand_query
        self._generate_insight = generate_insight
        self._provider_status = provider_status
        self._slots = asyncio.Semaphore(max_queue)
        self._max_queue = max_queue
        self._log = get_logger("insight.grpc")

    async def ExpandQuery(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.ExpandQueryRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.ExpandQueryResponse:
        """Расширяет запрос в поисковые фразы; отказ LLM даёт резервное расширение."""
        try:
            query_text = validate_query_text(request.query_text)
            expansion = await self._expand_query.execute(query_text)
        except AppError as error:
            await self._abort(context, error)
        return mappers.to_expand_response(expansion)

    async def JudgeCandidates(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.JudgeCandidatesRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.JudgeCandidatesResponse:
        """Смысловая оценка кандидатов; недоступность модели — пустой ответ с used_fallback, не ошибка."""
        if request.mode == "rubric_v2":
            return await self._judge_rubric(request)
        if self._judge_candidates is None or not request.items or not request.query_text.strip():
            return insight_pb2.JudgeCandidatesResponse(used_fallback=True)
        items = [
            JudgeItem(item.candidate_id, item.title, tuple(item.keyphrases), tuple(item.evidence))
            for item in request.items
        ]
        outcome = await self._judge_candidates.execute(request.query_text.strip()[:500], items)
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

    async def _judge_rubric(self, request: insight_pb2.JudgeCandidatesRequest) -> insight_pb2.JudgeCandidatesResponse:
        """Рубричная оценка (режим rubric_v2): код рубрики, четыре критерия, стадия, тренд, уверенность."""
        if self._rubric_judge is None or not request.items or not request.query_text.strip():
            return insight_pb2.JudgeCandidatesResponse(used_fallback=True)
        items = [
            RubricItem(item.candidate_id, item.title, tuple(item.keyphrases),
                       tuple(_source(source) for source in item.sources), item.composition_ru)
            for item in request.items
        ]
        outcome = await self._rubric_judge.execute(request.query_text.strip()[:500], items)
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

    async def FinalizeCards(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.FinalizeCardsRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.FinalizeCardsResponse:
        """Пакетная доводка карточек; недоступность модели — пустой ответ с used_fallback, не ошибка."""
        if self._finalize_cards is None or not request.cards or not request.query_text.strip():
            return insight_pb2.FinalizeCardsResponse(used_fallback=True)
        cards = [
            CardInput(card.candidate_id, card.title_auto, tuple(card.keyphrases),
                      tuple(_source(source) for source in card.sources), card.stage, card.trend, card.judge_reason_ru)
            for card in request.cards
        ]
        outcome = await self._finalize_cards.execute(request.query_text.strip()[:500], cards)
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

    async def GenerateInsight(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.GenerateInsightRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.GenerateInsightResponse:
        """Формирует нарратив кандидата по доказательствам."""
        if self._slots.locked():
            await self._abort(
                context,
                ResourceExhaustedError(
                    f"очередь генерации заполнена (предел {self._max_queue})", "QUEUE_FULL"
                ),
            )
        async with self._slots:
            try:
                command = mappers.from_generate_request(request)
                insight = await self._generate_insight.execute(command)
            except AppError as error:
                await self._abort(context, error)
        return mappers.to_generate_response(insight)

    async def GetProviderStatus(  # noqa: N802 - имя RPC из контракта
        self, request: insight_pb2.GetProviderStatusRequest, context: grpc.aio.ServicerContext
    ) -> insight_pb2.GetProviderStatusResponse:
        """Состояние провайдеров LLM."""
        states, active = self._provider_status.execute()
        return mappers.to_provider_status_response(states, active)

    async def _abort(self, context: grpc.aio.ServicerContext, error: AppError) -> None:
        """Переводит прикладную ошибку в статус gRPC с `error_code` в trailing metadata."""
        code = grpc.StatusCode.INTERNAL
        for error_type, status_code in _STATUS_BY_ERROR:
            if isinstance(error, error_type):
                code = status_code
                break
        self._log.warning(
            "rpc.rejected", error_code=error.error_code, code=code.name, message=error.message
        )
        await context.abort(code, error.message, (("error_code", error.error_code),))


def _source(source) -> RubricSource:  # noqa: ANN001 - insight_pb2.JudgeSource
    """Источник из контракта → источник рубричной оценки."""
    return RubricSource(
        document_id=source.document_id, title=source.title, source_key=source.source_key,
        source_type=source.source_type, trust_level=source.trust_level, published=source.published,
        language_code=source.language_code, snippet=source.snippet, domain=source.domain,
    )
