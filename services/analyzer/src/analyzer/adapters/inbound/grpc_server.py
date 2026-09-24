"""gRPC-сервер `AnalyzerService` (синхронный): валидация, вызов use cases, коды ошибок."""

from __future__ import annotations

import grpc
from weaksignals.analyzer.v1 import analyzer_pb2, analyzer_pb2_grpc
from weaksignals.common.v1 import common_pb2

from analyzer.adapters.inbound.mappers import (
    to_proto_analysis,
    to_proto_candidate,
    to_proto_model_info,
    to_proto_score_text,
    to_proto_status,
)
from analyzer.application.dto import StartAnalysisCommand
from analyzer.application.use_cases.cancel_analysis import CancelAnalysis
from analyzer.application.use_cases.get_analysis import GetAnalysis
from analyzer.application.use_cases.get_model_info import GetModelInfo
from analyzer.application.use_cases.list_candidates import ListCandidates
from analyzer.application.use_cases.score_text import ScoreText
from analyzer.application.use_cases.start_analysis import StartAnalysis
from analyzer.application.validation import (
    validate_cancel_reason,
    validate_idempotency_key,
    validate_page_size,
    validate_page_token,
    validate_params,
    validate_query_text,
    validate_score_text,
    validate_uuid,
)
from analyzer.domain.errors import (
    AppError,
    NotFoundError,
    PreconditionFailedError,
    ResourceExhaustedError,
    UnavailableError,
    ValidationError,
)
from ws_common.logging import get_logger

_STATUS_BY_ERROR: dict[type[AppError], grpc.StatusCode] = {
    ValidationError: grpc.StatusCode.INVALID_ARGUMENT,
    NotFoundError: grpc.StatusCode.NOT_FOUND,
    PreconditionFailedError: grpc.StatusCode.FAILED_PRECONDITION,
    ResourceExhaustedError: grpc.StatusCode.RESOURCE_EXHAUSTED,
    UnavailableError: grpc.StatusCode.UNAVAILABLE,
}


class AnalyzerServicer(analyzer_pb2_grpc.AnalyzerServiceServicer):
    """Транспортный слой: перевод proto ↔ домен и прикладных ошибок в статусы gRPC."""

    def __init__(
        self,
        start_analysis: StartAnalysis,
        get_analysis: GetAnalysis,
        list_candidates: ListCandidates,
        cancel_analysis: CancelAnalysis,
        score_text: ScoreText,
        get_model_info: GetModelInfo,
    ) -> None:
        self._start_analysis = start_analysis
        self._get_analysis = get_analysis
        self._list_candidates = list_candidates
        self._cancel_analysis = cancel_analysis
        self._score_text = score_text
        self._get_model_info = get_model_info
        self._log = get_logger("analyzer.grpc")

    def StartAnalysis(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.StartAnalysisRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.StartAnalysisResponse:
        """Идемпотентный запуск анализа коллекции."""
        try:
            command = StartAnalysisCommand(
                idempotency_key=validate_idempotency_key(request.idempotency_key),
                collection_id=validate_uuid(request.collection_id, "collection_id"),
                query_text=validate_query_text(request.query_text),
                params_raw=validate_params(
                    request.params.top_n,
                    request.params.max_candidates,
                    request.params.weak_signal_threshold,
                    request.params.min_evidence_documents,
                ),
            )
            result = self._start_analysis.execute(command)
        except AppError as error:
            self._abort(context, error)
        return analyzer_pb2.StartAnalysisResponse(
            analysis_id=result.analysis_id,
            status=to_proto_status(result.status),
            already_existed=result.already_existed,
            model_version_id=result.model_version_id,
        )

    def GetAnalysis(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.GetAnalysisRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.GetAnalysisResponse:
        """Статус и статистика анализа."""
        try:
            view = self._get_analysis.execute(validate_uuid(request.analysis_id, "analysis_id"))
        except AppError as error:
            self._abort(context, error)
        return to_proto_analysis(view)

    def ListCandidates(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.ListCandidatesRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.ListCandidatesResponse:
        """Кандидаты постранично: слабые сигналы по рангу, затем исключённые."""
        try:
            analysis_id = validate_uuid(request.analysis_id, "analysis_id")
            page = self._list_candidates.execute(
                analysis_id,
                include_excluded=request.include_excluded,
                page_size=validate_page_size(request.page.page_size),
                after=validate_page_token(request.page.page_token),
            )
        except AppError as error:
            self._abort(context, error)
        return analyzer_pb2.ListCandidatesResponse(
            candidates=[to_proto_candidate(candidate, analysis_id) for candidate in page.candidates],
            page=common_pb2.PageResponse(
                next_page_token=page.next_page_token, total_count=page.total_count
            ),
        )

    def CancelAnalysis(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.CancelAnalysisRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.CancelAnalysisResponse:
        """Кооперативная отмена анализа."""
        try:
            status = self._cancel_analysis.execute(
                validate_uuid(request.analysis_id, "analysis_id"),
                validate_cancel_reason(request.reason),
            )
        except AppError as error:
            self._abort(context, error)
        return analyzer_pb2.CancelAnalysisResponse(status=to_proto_status(status))

    def ScoreText(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.ScoreTextRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.ScoreTextResponse:
        """Прямой скоринг описания технологии."""
        try:
            title, description = validate_score_text(request.title, request.description)
            result = self._score_text.execute(title, description, request.with_enrichment)
        except AppError as error:
            self._abort(context, error)
        return to_proto_score_text(result)

    def GetModelInfo(  # noqa: N802 - имя RPC из контракта
        self, request: analyzer_pb2.GetModelInfoRequest, context: grpc.ServicerContext
    ) -> analyzer_pb2.GetModelInfoResponse:
        """Сведения об активной модели."""
        try:
            version, feature_names = self._get_model_info.execute()
        except AppError as error:
            self._abort(context, error)
        return to_proto_model_info(version, feature_names)

    def _abort(self, context: grpc.ServicerContext, error: AppError) -> None:
        """Переводит прикладную ошибку в статус gRPC с `error_code` в trailing metadata."""
        code = _STATUS_BY_ERROR.get(type(error), grpc.StatusCode.INTERNAL)
        for error_type, status_code in _STATUS_BY_ERROR.items():
            if isinstance(error, error_type):
                code = status_code
                break
        self._log.warning(
            "rpc.rejected", error_code=error.error_code, code=code.name, message=error.message
        )
        context.set_trailing_metadata((("error_code", error.error_code),))
        context.abort(code, error.message)
