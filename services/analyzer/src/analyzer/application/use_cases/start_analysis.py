"""Сценарий StartAnalysis: идемпотентный запуск анализа коллекции."""

from __future__ import annotations

from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import AnalysisDraft, StartAnalysisCommand, StartAnalysisResult
from analyzer.application.ports import AnalysisRepository, CollectorReader
from analyzer.domain.errors import CollectorUnavailable, PreconditionFailedError, ResourceExhaustedError
from analyzer.domain.values import AnalysisParams
from ws_common.logging import get_logger


class StartAnalysis:
    """Проверяет коллекцию и ставит анализ в очередь; повтор по ключу возвращает существующий."""

    def __init__(
        self,
        analyses: AnalysisRepository,
        collector: CollectorReader,
        active_model: ActiveModelHolder,
        max_pending: int,
    ) -> None:
        self._analyses = analyses
        self._collector = collector
        self._active_model = active_model
        self._max_pending = max_pending
        self._log = get_logger("analyzer.start_analysis")

    def execute(self, command: StartAnalysisCommand) -> StartAnalysisResult:
        """Создаёт анализ в статусе PENDING с активной моделью."""
        bundle = self._active_model.get()
        self._check_collection(command.collection_id)
        pending = self._analyses.count_pending()
        if pending >= self._max_pending:
            raise ResourceExhaustedError(
                f"очередь анализов заполнена ({pending} ≥ {self._max_pending})", "QUEUE_FULL"
            )
        top_n, max_candidates, threshold, min_evidence = command.params_raw
        analysis, already_existed = self._analyses.create_if_absent(
            AnalysisDraft(
                idempotency_key=command.idempotency_key,
                collection_id=command.collection_id,
                query_text=command.query_text,
                model_version_id=bundle.version.model_version_id,
                params=AnalysisParams.from_request(
                    bundle.threshold, top_n, max_candidates, threshold, min_evidence
                ),
            )
        )
        if not already_existed:
            self._log.info(
                "analysis.accepted",
                analysis_id=analysis.analysis_id,
                collection_id=command.collection_id,
                model_version_id=analysis.model_version_id,
            )
        return StartAnalysisResult(
            analysis_id=analysis.analysis_id,
            status=analysis.status,
            already_existed=already_existed,
            model_version_id=analysis.model_version_id,
        )

    def _check_collection(self, collection_id: str) -> None:
        """Коллекция должна быть в терминальном статусе и содержать хотя бы один документ."""
        try:
            info = self._collector.get_collection(collection_id)
        except CollectorUnavailable:
            raise
        if not info.is_terminal:
            raise PreconditionFailedError(
                f"коллекция {collection_id} ещё не завершена (статус {info.status})",
                "COLLECTION_NOT_TERMINAL",
            )
        if info.documents_total < 1:
            raise PreconditionFailedError(
                f"коллекция {collection_id} не содержит документов", "COLLECTION_EMPTY"
            )
