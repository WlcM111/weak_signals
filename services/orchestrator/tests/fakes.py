"""Дублёры портов orchestrator: часы, репозитории в памяти и клиенты трёх сервисов."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from orchestrator.application.dto import (
    CandidateView,
    JudgeVerdictView,
    AnalysisView,
    CandidateView,
    CollectionView,
    DocumentView,
    EvidenceView,
    ExpansionView,
    FeatureView,
    ModelInfoView,
    NarrativeView,
    ScoreTextView,
)
from orchestrator.domain.entities import ExcludedCandidate, Job, Query, ResultItem
from orchestrator.domain.errors import UpstreamUnavailable
from orchestrator.domain.values import (
    Decision,
    FeatureDirection,
    JobProgress,
    JobStats,
    JobStatus,
    Lease,
    NarrativeStatus,
    TrustLevel,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def new_id(seed: int) -> str:
    """Детерминированный UUID для тестов."""
    return str(uuid.UUID(int=seed, version=4))


class FakeClock:
    """Управляемые часы: время двигает тест, `sleep` не ждёт по-настоящему."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start
        self._monotonic = 0.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        """Текущее «время»."""
        return self._now

    def monotonic(self) -> float:
        """Монотонный счётчик секунд."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Пауза сдвигает часы вперёд без ожидания."""
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Сдвигает календарное и монотонное время."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class InMemoryJobRepository:
    """Реализация `JobRepository` в памяти с арендами и журналом переходов."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock or FakeClock()
        self.jobs: dict[str, Job] = {}
        self.queries: dict[str, Query] = {}
        self.events: list[tuple[str, str, str, str]] = []
        self.lease_alive = True
        self.postponements: dict[str, int] = {}
        self.available_at: dict[str, datetime] = {}
        self.max_postponements = 10

    async def insert(self, query, job, idempotency_key, request_hash, expires_at):  # noqa: ANN001, ANN201
        """Создаёт запрос и задание."""
        self.queries[query.query_id] = query
        job.created_at = self._clock.now()
        job.query_text = query.text
        job.requested_top_n = query.requested_top_n
        self.jobs[job.job_id] = job
        self.events.append((job.job_id, "", JobStatus.QUEUED.value, ""))
        return job

    async def get(self, job_id: str) -> Job | None:
        """Задание по идентификатору."""
        return self.jobs.get(job_id)

    async def list(self, *, limit, cursor, status):  # noqa: ANN001, ANN201
        """Список по убыванию времени создания."""
        rows = sorted(
            self.jobs.values(),
            key=lambda job: (job.created_at or NOW, job.job_id),
            reverse=True,
        )
        if status is not None:
            rows = [job for job in rows if job.status is status]
        if cursor is not None:
            rows = [job for job in rows if ((job.created_at or NOW), job.job_id) < cursor]
        return rows[:limit]

    async def count_pending(self) -> int:
        """Число незавершённых заданий."""
        return sum(1 for job in self.jobs.values() if not job.is_terminal)

    async def claim_next(self, owner: str, lease_seconds: int, now: datetime) -> Job | None:
        """Захват задания очереди или задания с истёкшей арендой; отложенные пропускаются."""
        for job in sorted(self.jobs.values(), key=lambda item: item.created_at or NOW):
            if job.cancel_requested or job.is_terminal:
                continue
            available_at = self.available_at.get(job.job_id)
            if available_at is not None and available_at > now:
                continue
            expired = job.status.is_running and job.lease is not None and job.lease.is_expired(now)
            if job.status is JobStatus.QUEUED or expired:
                if expired:
                    job.attempt += 1
                    job.status = JobStatus.QUEUED
                job.lease = Lease(owner=owner, expires_at=now + timedelta(seconds=lease_seconds))
                return job
        return None

    async def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> tuple[bool, bool]:
        """Продление аренды."""
        job = self.jobs.get(job_id)
        if job is None or not self.lease_alive:
            return False, False
        job.lease = Lease(owner=owner, expires_at=self._clock.now() + timedelta(seconds=lease_seconds))
        return True, job.cancel_requested

    async def transition(self, job: Job, target: JobStatus, worker_id: str, detail: str = "") -> None:
        """Переход статуса с записью события."""
        previous = job.status
        job.transition(target, self._clock.now())
        if detail and target is JobStatus.FAILED:
            job.error_message = detail[:2000]
            job.error_code = detail.split(":", 1)[0]
        elif detail and target is JobStatus.PARTIAL:
            job.error_message = detail
        self.events.append((job.job_id, previous.value, target.value, detail))

    async def set_references(self, job_id, collection_id="", analysis_id=""):  # noqa: ANN001, ANN201
        """Сохраняет идентификаторы коллекции и анализа."""
        job = self.jobs[job_id]
        if collection_id:
            job.collection_id = collection_id
        if analysis_id:
            job.analysis_id = analysis_id

    async def request_cancel(self, job_id: str) -> Job | None:
        """Запрос отмены."""
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.cancel_requested = True
        if job.status is JobStatus.QUEUED:
            job.cancel(self._clock.now())
        return job

    async def release_expired_leases(self, now: datetime) -> int:
        """Возврат заданий с истёкшей арендой."""
        released = 0
        for job in self.jobs.values():
            if job.status.is_running and job.lease is not None and job.lease.is_expired(now):
                if job.requeue(now):
                    released += 1
        return released

    async def postpone(self, job: Job, seconds: int, worker_id: str, reason: str) -> bool:
        """Откладывание без увеличения попытки: задание недоступно для захвата `seconds` секунд."""
        count = self.postponements.get(job.job_id, 0)
        if count >= self.max_postponements:
            return False
        self.postponements[job.job_id] = count + 1
        await self.transition(job, JobStatus.QUEUED, worker_id, f"POSTPONED:{reason}")
        self.available_at[job.job_id] = self._clock.now() + timedelta(seconds=seconds)
        return True


class InMemoryResultRepository:
    """Реализация `ResultRepository` в памяти."""

    def __init__(self, jobs: InMemoryJobRepository | None = None) -> None:
        self._jobs = jobs
        self.items: dict[str, list[ResultItem]] = {}
        self.excluded: dict[str, list[ExcludedCandidate]] = {}
        self.stats: dict[str, JobStats] = {}

    async def add_item(self, item: ResultItem) -> str:
        """Идемпотентная запись элемента."""
        rows = self.items.setdefault(item.job_id, [])
        for existing in rows:
            if existing.candidate_id == item.candidate_id:
                return existing.item_id
        item.item_id = new_id(len(rows) + 1000)
        rows.append(item)
        return item.item_id

    async def add_excluded(self, job_id: str, batch: Sequence[ExcludedCandidate]) -> int:
        """Запись исключённых кандидатов."""
        rows = self.excluded.setdefault(job_id, [])
        known = {item.candidate_id for item in rows}
        added = [item for item in batch if item.candidate_id not in known]
        rows.extend(added)
        return len(added)

    async def set_stats(self, job_id: str, stats: JobStats) -> None:
        """Сохраняет статистику."""
        self.stats[job_id] = stats

    async def get_stats(self, job_id: str) -> JobStats | None:
        """Статистика задания."""
        return self.stats.get(job_id)

    async def count_items(self, job_id: str) -> int:
        """Число записанных элементов."""
        return len(self.items.get(job_id, []))

    async def get_results(self, job_id):  # noqa: ANN001, ANN201
        """Снимок результата."""
        return (
            sorted(self.items.get(job_id, []), key=lambda item: item.rank),
            self.excluded.get(job_id, []),
            self.stats.get(job_id, JobStats()),
        )

    async def get_item(self, item_id: str) -> ResultItem | None:
        """Элемент по идентификатору."""
        for rows in self.items.values():
            for item in rows:
                if item.item_id == item_id:
                    return item
        return None

    async def progress(self, job_id: str) -> JobProgress | None:
        """Прогресс по записанным данным."""
        job = self._jobs.jobs.get(job_id) if self._jobs else None
        if job is None:
            return None
        stats = self.stats.get(job_id, JobStats())
        return JobProgress(
            stage=job.status,
            http_requests_total=stats.http_requests_total,
            sources_processed=stats.sources_processed,
            documents_collected=stats.documents_collected,
            candidates_found=stats.candidates_found,
            narratives_done=len(self.items.get(job_id, [])),
            narratives_total=stats.weak_signals_total,
        )


class InMemoryIdempotencyRepository:
    """Реализация `IdempotencyRepository` в памяти."""

    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, str, datetime]] = {}

    async def get(self, key: str) -> tuple[str, str] | None:
        """Ключ, если он есть и не истёк."""
        record = self.keys.get(key)
        return (record[0], record[1]) if record else None

    async def put(self, key: str, job_id: str, request_hash: str, expires_at: datetime) -> None:
        """Сохраняет ключ (используется дублёром репозитория заданий)."""
        self.keys[key] = (job_id, request_hash, expires_at)

    async def purge_expired(self, now: datetime) -> int:
        """Удаляет истёкшие ключи."""
        expired = [key for key, value in self.keys.items() if value[2] <= now]
        for key in expired:
            del self.keys[key]
        return len(expired)


class LinkedJobRepository(InMemoryJobRepository):
    """Репозиторий заданий, который дополнительно сохраняет ключ идемпотентности."""

    def __init__(self, idempotency: InMemoryIdempotencyRepository, clock: FakeClock | None = None) -> None:
        super().__init__(clock)
        self._idempotency = idempotency

    async def insert(self, query, job, idempotency_key, request_hash, expires_at):  # noqa: ANN001, ANN201
        """Создаёт задание и регистрирует ключ идемпотентности."""
        stored = await super().insert(query, job, idempotency_key, request_hash, expires_at)
        await self._idempotency.put(idempotency_key, stored.job_id, request_hash, expires_at)
        return stored


@dataclass
class FakeCollector:
    """Дублёр `CollectorClient` с управляемой последовательностью статусов."""

    documents_total: int = 120
    statuses: list[str] = field(default_factory=lambda: ["RUNNING", "COMPLETED"])
    documents: dict[str, DocumentView] = field(default_factory=dict)
    fail_start: bool = False
    fail_documents: bool = False
    started: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    collection_id: str = new_id(10)

    async def start_collection(self, idempotency_key, query_text, ru_terms, en_terms,
                               time_budget_seconds, max_total_documents):  # noqa: ANN001, ANN201
        """Запуск сбора."""
        if self.fail_start:
            raise UpstreamUnavailable("StartCollection: UNAVAILABLE")
        self.started.append(idempotency_key)
        return self.collection_id

    async def get_collection(self, collection_id: str) -> CollectionView:
        """Следующий статус из очереди; последний повторяется."""
        status = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
        return CollectionView(
            collection_id=collection_id,
            status=status,
            documents_total=self.documents_total,
            http_requests_total=1200,
            adapters_completed=6,
            failed_adapters=(),
            is_terminal=status in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"},
        )

    async def get_documents(self, document_ids: Sequence[str]) -> list[DocumentView]:
        """Документы по идентификаторам."""
        if self.fail_documents:
            raise UpstreamUnavailable("GetDocuments: UNAVAILABLE")
        return [self.documents[key] for key in document_ids if key in self.documents]

    async def cancel_collection(self, collection_id: str, reason: str) -> str:
        """Отмена сбора."""
        self.cancelled.append(collection_id)
        return "CANCELLED"


@dataclass
class FakeAnalyzer:
    """Дублёр `AnalyzerClient`."""

    candidates: list[CandidateView] = field(default_factory=list)
    statuses: list[str] = field(default_factory=lambda: ["RUNNING", "COMPLETED"])
    analysis_id: str = new_id(20)
    cancelled: list[str] = field(default_factory=list)
    model_version_id: str = "wsclf-2026.09.16-1"

    async def start_analysis(self, idempotency_key, collection_id, query_text, top_n):  # noqa: ANN001, ANN201
        """Запуск анализа."""
        return self.analysis_id

    async def get_analysis(self, analysis_id: str) -> AnalysisView:
        """Состояние анализа."""
        status = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
        weak = sum(1 for item in self.candidates if item.decision is Decision.WEAK_SIGNAL)
        return AnalysisView(
            analysis_id=analysis_id,
            status=status,
            model_version_id=self.model_version_id,
            candidates_scored=len(self.candidates),
            weak_signals_total=weak,
            weak_signals_confident=sum(
                1 for item in self.candidates
                if item.decision is Decision.WEAK_SIGNAL and item.score >= 0.75
            ),
            is_terminal=status in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"},
        )

    async def list_candidates(self, analysis_id, *, include_excluded, page_size, page_token):  # noqa: ANN001, ANN201
        """Страница кандидатов (одна страница в дублёре)."""
        rows = [
            item for item in self.candidates
            if include_excluded or item.decision is Decision.WEAK_SIGNAL
        ]
        return rows, ""

    async def cancel_analysis(self, analysis_id: str, reason: str) -> str:
        """Отмена анализа."""
        self.cancelled.append(analysis_id)
        return "CANCELLED"

    async def get_model_info(self) -> ModelInfoView:
        """Сведения о модели."""
        return ModelInfoView(
            model_version_id=self.model_version_id,
            model_family="logreg_elasticnet",
            feature_schema_version="v1",
            embedding_model="intfloat/multilingual-e5-base",
            dataset_version="ds-2026.09.16-v1",
            trained_at=NOW,
            metrics={"accuracy": 0.98, "f1": 0.98},
            feature_names=("lex_emergence_score",),
        )

    async def score_text(self, title, description, with_enrichment):  # noqa: ANN001, ANN201
        """Прямой скоринг."""
        return ScoreTextView(
            score=0.82,
            decision=Decision.WEAK_SIGNAL,
            decision_reason="MODEL_SCORE",
            features=(
                FeatureView("lex_emergence_score", "Лексика ранней стадии", 0.7, 0.4,
                            FeatureDirection.SUPPORTS_WEAK_SIGNAL),
            ),
            model_version_id=self.model_version_id,
            enrichment_applied=with_enrichment,
        )


@dataclass
class FakeInsight:
    """Дублёр `InsightClient`."""

    fail_expand: bool = False
    fail_generate: bool = False
    calls: list[str] = field(default_factory=list)
    # Правило смысловой оценки: кандидат → (вердикт, релевантность); None — оценка пустая, поведение прежнее.
    judge_rule: Callable[[CandidateView], tuple[str, int]] | None = None
    fail_judge: bool = False
    judge_calls: list[int] = field(default_factory=list)

    async def judge_candidates(self, query_text: str, candidates) -> dict[str, JudgeVerdictView]:  # noqa: ANN001
        """Смысловая оценка по правилу теста."""
        if self.fail_judge:
            raise UpstreamUnavailable("JudgeCandidates: UNAVAILABLE")
        self.judge_calls.append(len(candidates))
        if self.judge_rule is None:
            return {}
        return {
            c.candidate_id: JudgeVerdictView(c.candidate_id, *self.judge_rule(c), "Причина оценки.") for c in candidates
        }

    async def expand_query(self, query_text: str) -> ExpansionView:
        """Расширение запроса."""
        if self.fail_expand:
            raise UpstreamUnavailable("ExpandQuery: UNAVAILABLE")
        return ExpansionView(
            ru_terms=(query_text, f"{query_text} технологии"),
            en_terms=("weak signals", "emerging technologies"),
            domain_tags=("ai_infrastructure",),
            used_fallback=False,
        )

    async def generate_insight(  # noqa: ANN001, ANN201
        self, idempotency_key, candidate, query_text, evidence, prompt_version
    ):
        """Генерация нарратива."""
        if self.fail_generate:
            raise UpstreamUnavailable("GenerateInsight: UNAVAILABLE")
        self.calls.append(idempotency_key)
        return NarrativeView(
            title_ru=f"{candidate.title_auto} (нарратив)",
            description_ru="Описание технологии на русском языке.",
            advantage_ru="Потенциальное преимущество технологии.",
            case_example_ru="Кейс-пример применения технологии.",
            explanation_ru="Объяснение отнесения к слабому сигналу.",
            status=NarrativeStatus.GENERATED.value,
            llm_provider="gigachat",
            llm_model="GigaChat-2-Pro",
            prompt_version=prompt_version,
            case_document_id=evidence[0].document_id if evidence else "",
            source_summaries={
                document.document_id: ("Резюме источника на русском.", "GENERATIVE_SUMMARY")
                for document in evidence
            },
        )


def make_document(index: int, language: str = "en") -> DocumentView:
    """Документ-доказательство для тестов."""
    return DocumentView(
        document_id=new_id(100 + index),
        title=f"Документ {index}",
        url=f"https://example.org/{index}",
        text=(
            "Первое предложение исследования. Второе предложение с деталями эксперимента. "
            "Третье предложение с результатами."
        ),
        language_code=language,
        source_type="SCIENTIFIC_PUBLICATION",
        source_key="openalex",
        trust_level=TrustLevel.HIGH,
        published_at=NOW,
    )


def make_candidate(
    index: int,
    decision: Decision = Decision.WEAK_SIGNAL,
    rank: int = 0,
    score: float = 0.9,
    evidence_count: int = 2,
) -> CandidateView:
    """Кандидат analyzer для тестов."""
    return CandidateView(
        candidate_id=new_id(200 + index),
        rank=rank,
        title_auto=f"Кандидат {index}",
        keyphrases=(f"фраза {index}",),
        score=score,
        decision=decision,
        decision_reason="MODEL_SCORE" if decision is Decision.WEAK_SIGNAL else "MATURITY_LEXICON",
        decision_explanation_ru="Объяснение решения по кандидату.",
        document_count=5,
        features=(
            FeatureView("lex_emergence_score", "Лексика ранней стадии", 0.8, 0.42,
                        FeatureDirection.SUPPORTS_WEAK_SIGNAL),
            FeatureView("trusted_share", "Доля доверенных источников", 0.6, 0.11,
                        FeatureDirection.SUPPORTS_WEAK_SIGNAL),
        ),
        evidence=tuple(
            EvidenceView(
                document_id=new_id(100 + position),
                snippet=f"Сниппет доказательства {position}.",
                similarity=0.9 - position * 0.05,
                source_type="SCIENTIFIC_PUBLICATION",
                trust_level=TrustLevel.HIGH,
            )
            for position in range(1, evidence_count + 1)
        ),
    )
