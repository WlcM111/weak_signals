"""Асинхронные gRPC-клиенты к collector, analyzer и insight с дедлайнами `CONTRACT_RULES`."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

import grpc
from weaksignals.analyzer.v1 import analyzer_pb2, analyzer_pb2_grpc
from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc
from weaksignals.common.v1 import common_pb2
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

from orchestrator.adapters.outbound.grpc import mappers
from orchestrator.application.dto import (
    FinalizedCardView,
    FinalizeRequestCard,
    JudgeVerdictView,
    RubricRequestItem,
    AnalysisView,
    CandidateView,
    CollectionView,
    DocumentView,
    ExpansionView,
    ModelInfoView,
    NarrativeView,
    ScoreTextView,
)
from orchestrator.domain.errors import UpstreamUnavailable
from orchestrator.domain.values import NarrativeStatus, SummaryKind
from ws_common.ids import current_correlation_id
from ws_common.logging import get_logger

START_DEADLINE = 5.0
GET_DEADLINE = 5.0
DOCUMENTS_DEADLINE = 15.0
CANDIDATES_DEADLINE = 15.0
EXPAND_DEADLINE = 30.0
INSIGHT_DEADLINE = 120.0
# Один пакетный вызов LLM на до 30 кандидатов; GigaChat отвечает на такой запрос за 10–60 с.
JUDGE_DEADLINE = 150.0
# Рубричная оценка идёт пачками по 8 кандидатов, доводка — по 5 карточек: до 5 и 3 последовательных вызовов LLM.
RUBRIC_DEADLINE = 420.0
FINALIZE_DEADLINE = 420.0
log = get_logger("orchestrator.upstream")


def _md() -> tuple[tuple[str, str], ...]:
    """Метаданные исходящего вызова: сквозной correlation id и имя вызывающего сервиса (§10.2 ТЗ)."""
    return (("x-correlation-id", current_correlation_id()), ("x-caller", "orchestrator"))


def _fail(rpc: str, error: grpc.aio.AioRpcError) -> UpstreamUnavailable:
    """Переводит отказ gRPC в доменную ошибку без раскрытия внутренних деталей."""
    log.warning("upstream.call", rpc=rpc, grpc_code=error.code().name)
    return UpstreamUnavailable(f"{rpc}: {error.code().name}")


class GrpcCollectorClient:
    """Реализация порта `CollectorClient`."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._stub = collector_pb2_grpc.CollectorServiceStub(channel)

    async def start_collection(
        self,
        idempotency_key: str,
        query_text: str,
        ru_terms: Sequence[str],
        en_terms: Sequence[str],
        time_budget_seconds: int,
        max_total_documents: int,
    ) -> str:
        """Запускает сбор в режиме SEARCH."""
        request = collector_pb2.StartCollectionRequest(
            idempotency_key=idempotency_key,
            query_text=query_text,
            terms=collector_pb2.SearchTerms(ru=list(ru_terms), en=list(en_terms)),
            mode=collector_pb2.COLLECTION_MODE_SEARCH,
            limits=collector_pb2.CollectionLimits(
                time_budget_seconds=time_budget_seconds, max_total_documents=max_total_documents
            ),
        )
        try:
            response = await self._stub.StartCollection(request, timeout=START_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("StartCollection", error) from error
        return response.collection_id

    async def get_collection(self, collection_id: str) -> CollectionView:
        """Состояние коллекции."""
        try:
            response = await self._stub.GetCollection(
                collector_pb2.GetCollectionRequest(collection_id=collection_id), timeout=GET_DEADLINE, metadata=_md()
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("GetCollection", error) from error
        return mappers.to_collection_view(response)

    async def get_documents(self, document_ids: Sequence[str]) -> list[DocumentView]:
        """Документы по идентификаторам."""
        try:
            response = await self._stub.GetDocuments(
                collector_pb2.GetDocumentsRequest(document_ids=list(document_ids)),
                timeout=DOCUMENTS_DEADLINE, metadata=_md(),
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("GetDocuments", error) from error
        return [mappers.to_document_view(document) for document in response.documents]

    async def cancel_collection(self, collection_id: str, reason: str) -> str:
        """Отмена сбора."""
        try:
            response = await self._stub.CancelCollection(
                collector_pb2.CancelCollectionRequest(collection_id=collection_id, reason=reason),
                timeout=GET_DEADLINE, metadata=_md(),
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("CancelCollection", error) from error
        return mappers.status_name(response.status)


class GrpcAnalyzerClient:
    """Реализация порта `AnalyzerClient`."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._stub = analyzer_pb2_grpc.AnalyzerServiceStub(channel)

    async def start_analysis(
        self, idempotency_key: str, collection_id: str, query_text: str, top_n: int
    ) -> str:
        """Запускает анализ коллекции."""
        request = analyzer_pb2.StartAnalysisRequest(
            idempotency_key=idempotency_key,
            collection_id=collection_id,
            query_text=query_text,
            params=analyzer_pb2.AnalysisParams(top_n=top_n),
        )
        try:
            response = await self._stub.StartAnalysis(request, timeout=START_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("StartAnalysis", error) from error
        return response.analysis_id

    async def get_analysis(self, analysis_id: str) -> AnalysisView:
        """Состояние анализа."""
        try:
            response = await self._stub.GetAnalysis(
                analyzer_pb2.GetAnalysisRequest(analysis_id=analysis_id), timeout=GET_DEADLINE, metadata=_md()
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("GetAnalysis", error) from error
        return mappers.to_analysis_view(response)

    async def list_candidates(
        self, analysis_id: str, *, include_excluded: bool, page_size: int, page_token: str
    ) -> tuple[list[CandidateView], str]:
        """Страница кандидатов."""
        request = analyzer_pb2.ListCandidatesRequest(
            analysis_id=analysis_id,
            include_excluded=include_excluded,
            page=common_pb2.PageRequest(page_size=page_size, page_token=page_token),
        )
        try:
            response = await self._stub.ListCandidates(request, timeout=CANDIDATES_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("ListCandidates", error) from error
        return (
            [mappers.to_candidate_view(candidate) for candidate in response.candidates],
            response.page.next_page_token,
        )

    async def cancel_analysis(self, analysis_id: str, reason: str) -> str:
        """Отмена анализа."""
        try:
            response = await self._stub.CancelAnalysis(
                analyzer_pb2.CancelAnalysisRequest(analysis_id=analysis_id, reason=reason),
                timeout=GET_DEADLINE, metadata=_md(),
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("CancelAnalysis", error) from error
        return mappers.status_name(response.status)

    async def get_model_info(self) -> ModelInfoView:
        """Сведения об активной модели."""
        try:
            response = await self._stub.GetModelInfo(
                analyzer_pb2.GetModelInfoRequest(), timeout=GET_DEADLINE, metadata=_md()
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("GetModelInfo", error) from error
        return mappers.to_model_info_view(response)

    async def score_text(self, title: str, description: str, with_enrichment: bool) -> ScoreTextView:
        """Прямой скоринг описания технологии."""
        request = analyzer_pb2.ScoreTextRequest(
            title=title, description=description, with_enrichment=with_enrichment
        )
        try:
            response = await self._stub.ScoreText(request, timeout=INSIGHT_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("ScoreText", error) from error
        return mappers.to_score_view(response)


class GrpcInsightClient:
    """Реализация порта `InsightClient`."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._stub = insight_pb2_grpc.InsightServiceStub(channel)

    async def expand_query(self, query_text: str) -> ExpansionView:
        """Расширение запроса в поисковые термины."""
        try:
            response = await self._stub.ExpandQuery(
                insight_pb2.ExpandQueryRequest(query_text=query_text), timeout=EXPAND_DEADLINE, metadata=_md()
            )
        except grpc.aio.AioRpcError as error:
            raise _fail("ExpandQuery", error) from error
        return ExpansionView(
            ru_terms=tuple(response.ru_terms),
            en_terms=tuple(response.en_terms),
            domain_tags=tuple(response.domain_tags),
            used_fallback=response.used_fallback,
        )

    async def judge_candidates(
        self, query_text: str, candidates: Sequence[CandidateView]
    ) -> dict[str, JudgeVerdictView]:
        """Смысловая оценка кандидатов; ответ с used_fallback даёт пустой словарь."""
        request = insight_pb2.JudgeCandidatesRequest(
            query_text=query_text,
            items=[
                insight_pb2.JudgeItem(
                    candidate_id=candidate.candidate_id,
                    title=candidate.title_auto,
                    keyphrases=list(candidate.keyphrases[:8]),
                    evidence=[item.snippet[:300] for item in candidate.evidence[:3]],
                )
                for candidate in candidates
            ],
        )
        try:
            response = await self._stub.JudgeCandidates(request, timeout=JUDGE_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("JudgeCandidates", error) from error
        return {
            verdict.candidate_id: JudgeVerdictView(
                verdict.candidate_id, verdict.verdict, verdict.relevance, verdict.reason_ru
            )
            for verdict in response.verdicts
        }

    async def judge_rubric(
        self, query_text: str, items: Sequence[RubricRequestItem]
    ) -> dict[str, JudgeVerdictView]:
        """Рубричная оценка (режим rubric_v2); ответ с used_fallback даёт пустой словарь."""
        request = insight_pb2.JudgeCandidatesRequest(
            query_text=query_text,
            mode="rubric_v2",
            items=[
                insight_pb2.JudgeItem(
                    candidate_id=item.candidate.candidate_id,
                    title=item.candidate.title_auto,
                    keyphrases=list(item.candidate.keyphrases[:8]),
                    sources=[_judge_source(document, 400) for document in item.documents[:6]],
                    composition_ru=item.composition_ru[:600],
                )
                for item in items
            ],
        )
        try:
            response = await self._stub.JudgeCandidates(request, timeout=RUBRIC_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("JudgeCandidates", error) from error
        return {
            v.candidate_id: JudgeVerdictView(
                v.candidate_id, v.verdict, v.relevance, v.reason_ru, code=v.code, on_topic=v.on_topic,
                concrete=v.concrete, early_stage=v.early_stage, verifiable=v.verifiable, stage=v.stage,
                trend=v.trend, confidence=round(float(v.confidence), 4),
            )
            for v in response.verdicts
            if v.code and 1 <= v.stage <= 4 and 1 <= v.trend <= 3
        }

    async def finalize_cards(
        self, query_text: str, cards: Sequence[FinalizeRequestCard]
    ) -> dict[str, FinalizedCardView]:
        """Пакетная доводка карточек; в ответе только карточки, прошедшие проверку по источникам."""
        request = insight_pb2.FinalizeCardsRequest(
            query_text=query_text,
            cards=[
                insight_pb2.FinalizeCard(
                    candidate_id=card.candidate.candidate_id,
                    title_auto=card.candidate.title_auto,
                    keyphrases=list(card.candidate.keyphrases[:8]),
                    sources=[_judge_source(document, 1500) for document in card.documents[:5]],
                    stage=card.stage,
                    trend=card.trend,
                    judge_reason_ru=card.judge_reason_ru[:300],
                )
                for card in cards
            ],
        )
        try:
            response = await self._stub.FinalizeCards(request, timeout=FINALIZE_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("FinalizeCards", error) from error
        return {
            card.candidate_id: FinalizedCardView(
                candidate_id=card.candidate_id, title_ru=card.title_ru, description_ru=card.description_ru,
                advantage_ru=card.advantage_ru, case_example_ru=card.case_example_ru,
                case_document_id=card.case_document_id, why_ru=card.why_ru, companies=tuple(card.companies),
                stage=card.stage, trend=card.trend, stage_reason_ru=card.stage_reason_ru,
                trend_reason_ru=card.trend_reason_ru,
                source_summaries={
                    summary.document_id: (summary.summary_ru, insight_pb2.SummaryKind.Name(summary.kind).removeprefix("SUMMARY_KIND_"))
                    for summary in card.source_summaries
                },
                llm_provider=response.provider, llm_model=response.model,
                prompt_version=response.prompt_version or "finalize_v1",
            )
            for card in response.cards
            if card.title_ru
        }

    async def generate_insight(
        self,
        idempotency_key: str,
        candidate: CandidateView,
        query_text: str,
        evidence: Sequence[DocumentView],
        prompt_version: str,
    ) -> NarrativeView:
        """Генерация нарратива кандидата на переданных доказательствах."""
        request = insight_pb2.GenerateInsightRequest(
            idempotency_key=idempotency_key,
            candidate=insight_pb2.CandidateContext(
                candidate_id=candidate.candidate_id,
                title=candidate.title_auto,
                keyphrases=list(candidate.keyphrases),
                score=candidate.score,
                decision=analyzer_pb2.Decision.Value(f"DECISION_{candidate.decision.name}"),
                top_features=[
                    analyzer_pb2.FeatureContribution(
                        feature_name=feature.feature_name,
                        value=feature.value,
                        contribution=feature.contribution,
                        label_ru=feature.label_ru,
                        direction=feature.direction.value,
                    )
                    for feature in candidate.features[:8]
                ],
                query_text=query_text,
            ),
            evidence=[
                insight_pb2.EvidenceDocument(
                    document_id=document.document_id,
                    title=document.title,
                    url=document.url,
                    text=document.text,
                    language_code=document.language_code,
                    source_type=common_pb2.SourceType.Value(f"SOURCE_TYPE_{document.source_type}"),
                    trust_level=common_pb2.TrustLevel.Value(f"TRUST_LEVEL_{document.trust_level.value}"),
                )
                for document in evidence
            ],
            allow_fallback=True,
        )
        try:
            response = await self._stub.GenerateInsight(request, timeout=INSIGHT_DEADLINE, metadata=_md())
        except grpc.aio.AioRpcError as error:
            raise _fail("GenerateInsight", error) from error
        narrative = response.narrative
        provenance = response.provenance
        status = (
            NarrativeStatus.GENERATED
            if insight_pb2.InsightStatus.Name(response.status) == "INSIGHT_STATUS_GENERATED"
            else NarrativeStatus.FALLBACK_EXTRACTIVE
        )
        return NarrativeView(
            title_ru=narrative.title_ru,
            description_ru=narrative.description_ru,
            advantage_ru=narrative.advantage_ru,
            case_example_ru=narrative.case_example_ru,
            explanation_ru=narrative.explanation_ru,
            status=status.value,
            llm_provider=provenance.provider,
            llm_model=provenance.model,
            prompt_version=provenance.prompt_version or prompt_version,
            case_document_id=narrative.case_document_id,
            source_summaries={
                summary.document_id: (
                    summary.summary_ru,
                    SummaryKind[
                        insight_pb2.SummaryKind.Name(summary.kind).removeprefix("SUMMARY_KIND_")
                    ].value,
                )
                for summary in response.source_summaries
            },
        )


def _judge_source(document: DocumentView, snippet_chars: int):  # noqa: ANN202 - insight_pb2.JudgeSource
    """Документ → источник для рубричной оценки и доводки (полный контекст, фрагмент ограничен)."""
    published = document.published_at.date().isoformat() if document.published_at else ""
    host = urlsplit(document.url or "").hostname or ""
    return insight_pb2.JudgeSource(
        document_id=document.document_id,
        title=(document.title or "")[:300],
        source_key=document.source_key,
        source_type=document.source_type,
        trust_level=str(getattr(document.trust_level, "value", document.trust_level)),
        published=published,
        language_code=document.language_code,
        snippet=" ".join((document.text or "").split())[:snippet_chars],
        domain=host.removeprefix("www."),
    )
