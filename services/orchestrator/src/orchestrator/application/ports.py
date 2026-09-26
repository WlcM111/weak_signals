"""Порты orchestrator (`typing.Protocol`). Асинхронные: FastAPI, psycopg async, grpc.aio (ADR-09)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

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
from orchestrator.domain.entities import ExcludedCandidate, Job, Query, ResultItem
from orchestrator.domain.values import JobProgress, JobStats, JobStatus


class JobRepository(Protocol):
    """Хранилище заданий и очередь с арендами (таблицы `jobs`, `job_events`)."""

    async def insert(self, query: Query, job: Job, idempotency_key: str, request_hash: str,
                     expires_at: datetime) -> Job:
        """Одной транзакцией создаёт запрос, задание, ключ идемпотентности и событие перехода."""

    async def get(self, job_id: str) -> Job | None:
        """Задание по идентификатору (с текстом запроса)."""

    async def list(self, *, limit: int, cursor: tuple[datetime, str] | None,
                   status: JobStatus | None) -> list[Job]:
        """Страница заданий по убыванию `created_at`."""

    async def count_pending(self) -> int:
        """Число незавершённых заданий (для лимита очереди)."""

    async def claim_next(self, owner: str, lease_seconds: int, now: datetime) -> Job | None:
        """Захватывает QUEUED или задание с истёкшей арендой (`FOR UPDATE SKIP LOCKED`)."""

    async def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> tuple[bool, bool]:
        """Продлевает аренду; возвращает (аренда жива, запрошена ли отмена)."""

    async def transition(self, job: Job, target: JobStatus, worker_id: str, detail: str = "") -> None:
        """Сохраняет переход статуса вместе с записью в журнал событий."""

    async def set_references(self, job_id: str, collection_id: str = "", analysis_id: str = "") -> None:
        """Сохраняет идентификаторы коллекции и анализа для повторного использования."""

    async def request_cancel(self, job_id: str) -> Job | None:
        """Ставит признак отмены; QUEUED сразу переводит в CANCELLED."""

    async def release_expired_leases(self, now: datetime) -> int:
        """Возвращает задания с истёкшей арендой в очередь (увеличивая попытку)."""

    async def postpone(self, job: Job, seconds: int, worker_id: str, reason: str) -> bool:
        """Откладывает задание в QUEUED без увеличения попытки; False при исчерпании лимита."""


class ResultRepository(Protocol):
    """Снимок результата (таблицы `result_items`, `*_features`, `*_sources`, `excluded_candidates`)."""

    async def add_item(self, item: ResultItem) -> str:
        """Транзакционно пишет элемент с признаками и источниками (`ON CONFLICT DO NOTHING`)."""

    async def add_excluded(self, job_id: str, batch: Sequence[ExcludedCandidate]) -> int:
        """Пишет исключённых кандидатов пачкой."""

    async def set_stats(self, job_id: str, stats: JobStats) -> None:
        """Сохраняет статистику задания."""

    async def get_stats(self, job_id: str) -> JobStats | None:
        """Статистика задания, если она записана."""

    async def count_items(self, job_id: str) -> int:
        """Число уже записанных элементов (прогрессивная выдача и идемпотентность)."""

    async def get_results(self, job_id: str) -> tuple[list[ResultItem], list[ExcludedCandidate], JobStats]:
        """Снимок результата задания целиком."""

    async def get_item(self, item_id: str) -> ResultItem | None:
        """Элемент выдачи по идентификатору."""

    async def progress(self, job_id: str) -> JobProgress | None:
        """Прогресс задания по записанным данным."""


class IdempotencyRepository(Protocol):
    """Ключи идемпотентности HTTP (таблица `idempotency_keys`)."""

    async def get(self, key: str) -> tuple[str, str] | None:
        """Возвращает (`job_id`, `request_hash`) для ключа, если он не истёк."""

    async def purge_expired(self, now: datetime) -> int:
        """Удаляет истёкшие ключи."""


class CollectorClient(Protocol):
    """Клиент `collector.CollectorService`."""

    async def start_collection(self, idempotency_key: str, query_text: str, ru_terms: Sequence[str],
                               en_terms: Sequence[str], time_budget_seconds: int,
                               max_total_documents: int) -> str:
        """Запускает сбор в режиме SEARCH и возвращает `collection_id`."""

    async def get_collection(self, collection_id: str) -> CollectionView:
        """Состояние коллекции."""

    async def get_documents(self, document_ids: Sequence[str]) -> list[DocumentView]:
        """Документы по идентификаторам (≤ 200 за вызов)."""

    async def cancel_collection(self, collection_id: str, reason: str) -> str:
        """Отменяет сбор; возвращает итоговый статус."""


class AnalyzerClient(Protocol):
    """Клиент `analyzer.AnalyzerService`."""

    async def start_analysis(self, idempotency_key: str, collection_id: str, query_text: str,
                             top_n: int) -> str:
        """Запускает анализ и возвращает `analysis_id`."""

    async def get_analysis(self, analysis_id: str) -> AnalysisView:
        """Состояние анализа."""

    async def list_candidates(self, analysis_id: str, *, include_excluded: bool, page_size: int,
                              page_token: str) -> tuple[list[CandidateView], str]:
        """Страница кандидатов и токен следующей страницы."""

    async def cancel_analysis(self, analysis_id: str, reason: str) -> str:
        """Отменяет анализ."""

    async def get_model_info(self) -> ModelInfoView:
        """Сведения об активной модели."""

    async def score_text(self, title: str, description: str, with_enrichment: bool) -> ScoreTextView:
        """Прямой скоринг описания технологии."""


class InsightClient(Protocol):
    """Клиент `insight.InsightService`."""

    async def expand_query(self, query_text: str) -> ExpansionView:
        """Расширяет запрос в поисковые термины ru/en."""

    async def generate_insight(self, idempotency_key: str, candidate: CandidateView, query_text: str,
                               evidence: Sequence[DocumentView], prompt_version: str) -> NarrativeView:
        """Генерирует нарратив кандидата на переданных доказательствах."""

    async def judge_candidates(
        self, query_text: str, candidates: Sequence[CandidateView]
    ) -> dict[str, JudgeVerdictView]:
        """Смысловая оценка кандидатов; пустой словарь — оценка недоступна."""

    async def judge_rubric(
        self, query_text: str, items: Sequence[RubricRequestItem]
    ) -> dict[str, JudgeVerdictView]:
        """Рубричная оценка кандидатов с полным контекстом источников (режим отбора rubric)."""

    async def finalize_cards(
        self, query_text: str, cards: Sequence[FinalizeRequestCard]
    ) -> dict[str, FinalizedCardView]:
        """Пакетная доводка показанных карточек; не прошедшие проверку карточки в ответ не входят."""


class MetricsSink(Protocol):
    """Приём метрик orchestrator."""

    def job_finished(self, status: JobStatus) -> None:
        """Завершение задания."""

    def stage_duration(self, stage: str, seconds: float) -> None:
        """Длительность стадии."""

    def queue_pending(self, count: int) -> None:
        """Текущая длина очереди."""

    def http_request(self, path: str, status: int) -> None:
        """HTTP-запрос к API."""

    def upstream_call(self, rpc: str, code: str) -> None:
        """Вызов внешнего сервиса."""


class NullMetrics:
    """Метрики отключены (тесты)."""

    def job_finished(self, status: JobStatus) -> None:
        """Ничего не делает."""

    def stage_duration(self, stage: str, seconds: float) -> None:
        """Ничего не делает."""

    def queue_pending(self, count: int) -> None:
        """Ничего не делает."""

    def http_request(self, path: str, status: int) -> None:
        """Ничего не делает."""

    def upstream_call(self, rpc: str, code: str) -> None:
        """Ничего не делает."""
