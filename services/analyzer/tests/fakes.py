"""Тестовые дублёры портов analyzer: детерминированные часы, эмбеддер, collector и репозитории."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import numpy as np

from analyzer.application.dto import (
    AnalysisDraft,
    CachedEmbedding,
    CollectionInfo,
    EncyclopediaHit,
    LeaseState,
)
from analyzer.domain.entities import Analysis, Candidate, DocumentRef, ModelVersion
from analyzer.domain.values import AnalysisStats, Decision, OperationStatus, SourceType, TrustLevel

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
EMBEDDING_DIMS = 8


class FakeClock:
    """Управляемые часы: время двигает тест, не системный таймер."""

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

    def sleep(self, seconds: float) -> None:
        """Пауза сдвигает часы вперёд без реального ожидания."""
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Сдвигает и календарное, и монотонное время."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class FakeEmbedder:
    """Детерминированный эмбеддер: вектор зависит только от текста (hash → генератор)."""

    model_name_value = "intfloat/multilingual-e5-base"

    def __init__(
        self,
        dims: int = EMBEDDING_DIMS,
        topic_terms: dict[str, int] | None = None,
        base_axis: int = 0,
        base_weight: float = 0.0,
        jitter: float = 0.0,
    ) -> None:
        self._dims = dims
        self._topics = topic_terms or {}
        self._base_axis = base_axis
        self._base_weight = base_weight
        self._jitter = jitter
        self.calls: list[tuple[int, str]] = []
        self.encoded_texts: list[str] = []

    @property
    def model_name(self) -> str:
        """Имя модели, совпадающее с манифестом фикстуры."""
        return self.model_name_value

    @property
    def dims(self) -> int:
        """Размерность вектора."""
        return self._dims

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов: тема задаёт направление, остальное — небольшой шум."""
        self.calls.append((len(texts), prefix))
        vectors = np.zeros((len(texts), self._dims), dtype=np.float32)
        for row, text in enumerate(texts):
            lowered = text.lower()
            self.encoded_texts.append(text)
            axis = next(
                (index for term, index in self._topics.items() if term in lowered),
                int.from_bytes(hashlib.blake2b(lowered.encode(), digest_size=4).digest(), "big") % self._dims,
            )
            vectors[row, axis % self._dims] = 1.0
            # общая «тема запроса»: без неё все группы были бы ортогональны запросу
            vectors[row, self._base_axis % self._dims] += self._base_weight
            seed = int.from_bytes(hashlib.blake2b(lowered.encode(), digest_size=4).digest(), "big")
            generator = np.random.default_rng(seed)
            noise = generator.normal(size=self._dims).astype(np.float32)
            noise /= max(float(np.linalg.norm(noise)), 1e-9)
            # индивидуальное отклонение документа: реальные эмбеддинги не совпадают внутри темы
            vectors[row] += (self._jitter or 0.005) * noise
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return vectors / norms


@dataclass
class FakeCollector:
    """Дублёр clients collector: документы из памяти, фиксированные ответы энциклопедии."""

    documents: list[DocumentRef] = field(default_factory=list)
    collection: CollectionInfo | None = None
    encyclopedia: dict[str, EncyclopediaHit] = field(default_factory=dict)
    enrichment_collection_id: str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    stream_calls: int = 0
    encyclopedia_calls: int = 0

    def get_collection(self, collection_id: str) -> CollectionInfo:
        """Сведения о коллекции."""
        return self.collection or CollectionInfo(
            collection_id=collection_id,
            status="COMPLETED",
            documents_total=len(self.documents),
            is_terminal=True,
        )

    def stream_documents(self, collection_id: str, chunk_size: int, limit: int) -> Iterator[DocumentRef]:
        """Документы коллекции с учётом лимита."""
        self.stream_calls += 1
        yield from self.documents[:limit]

    def check_encyclopedia(self, titles: Sequence[str], language_code: str) -> list[EncyclopediaHit]:
        """Ответ по каждому названию (по умолчанию — статьи нет)."""
        self.encyclopedia_calls += 1
        return [
            self.encyclopedia.get(title.lower(), EncyclopediaHit(title=title, exists=False))
            for title in titles
        ]

    def start_enrichment(self, idempotency_key: str, title: str) -> str:
        """Идентификатор ENRICHMENT-коллекции."""
        return self.enrichment_collection_id


class InMemoryAnalysisRepository:
    """Реализация `AnalysisRepository` в памяти с арендами и очередью."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock or FakeClock()
        self.items: dict[str, Analysis] = {}
        self._by_key: dict[str, str] = {}
        self.heartbeats: int = 0
        self.lease_alive = True

    def create_if_absent(self, draft: AnalysisDraft) -> tuple[Analysis, bool]:
        """Создаёт анализ или возвращает существующий по ключу идемпотентности."""
        existing_id = self._by_key.get(draft.idempotency_key)
        if existing_id:
            return self.items[existing_id], True
        analysis = Analysis(
            analysis_id=str(uuid.uuid4()),
            idempotency_key=draft.idempotency_key,
            collection_id=draft.collection_id,
            query_text=draft.query_text,
            model_version_id=draft.model_version_id,
            params=draft.params,
            created_at=self._clock.now(),
        )
        self.items[analysis.analysis_id] = analysis
        self._by_key[draft.idempotency_key] = analysis.analysis_id
        return analysis, False

    def add(self, analysis: Analysis) -> Analysis:
        """Помещает готовую сущность в хранилище (для тестов)."""
        self.items[analysis.analysis_id] = analysis
        self._by_key[analysis.idempotency_key] = analysis.analysis_id
        return analysis

    def get(self, analysis_id: str) -> Analysis | None:
        """Анализ по идентификатору."""
        return self.items.get(analysis_id)

    def count_pending(self) -> int:
        """Число незавершённых анализов."""
        return sum(1 for item in self.items.values() if not item.is_terminal)

    def claim_next(self, owner: str, lease_seconds: int) -> Analysis | None:
        """Первый PENDING по времени создания."""
        for analysis in sorted(self.items.values(), key=lambda item: item.created_at or self._clock.now()):
            if analysis.status is OperationStatus.PENDING and not analysis.cancel_requested:
                analysis.transition_to(OperationStatus.RUNNING, self._clock.now())
                return analysis
        return None

    def heartbeat(self, analysis_id: str, owner: str, lease_seconds: int) -> LeaseState:
        """Продление аренды с учётом флага отмены."""
        self.heartbeats += 1
        analysis = self.items.get(analysis_id)
        if analysis is None or not self.lease_alive:
            return LeaseState(alive=False, cancel_requested=False)
        return LeaseState(alive=True, cancel_requested=analysis.cancel_requested)

    def request_cancel(self, analysis_id: str) -> OperationStatus | None:
        """Запрос отмены."""
        analysis = self.items.get(analysis_id)
        if analysis is None:
            return None
        analysis.cancel_requested = True
        if analysis.status is OperationStatus.PENDING:
            analysis.finish(OperationStatus.CANCELLED, self._clock.now(), error_code="CANCELLED")
        return analysis.status

    def finish(
        self,
        analysis_id: str,
        owner: str,
        status: OperationStatus,
        *,
        stats: AnalysisStats,
        error_code: str,
        error_message: str,
        finished_at: datetime,
    ) -> bool:
        """Терминальное завершение."""
        analysis = self.items.get(analysis_id)
        if analysis is None or analysis.is_terminal:
            return False
        analysis.finish(status, finished_at, stats, error_code, error_message)
        return True

    def release_expired_leases(self) -> int:
        """Аренды в памяти не истекают сами."""
        return 0


class InMemoryCandidateRepository:
    """Реализация `CandidateRepository` в памяти."""

    def __init__(self, analyses: InMemoryAnalysisRepository, clock: FakeClock | None = None) -> None:
        self._analyses = analyses
        self._clock = clock or FakeClock()
        self.saved: dict[str, list[Candidate]] = {}
        self.save_calls: int = 0
        self.lease_alive = True

    def save_results(
        self,
        analysis_id: str,
        owner: str,
        candidates: Sequence[Candidate],
        stats: AnalysisStats,
        finished_at: datetime,
    ) -> bool:
        """Заменяет кандидатов и завершает анализ, если аренда жива."""
        self.save_calls += 1
        if not self.lease_alive:
            return False
        analysis = self._analyses.get(analysis_id)
        if analysis is None:
            return False
        for index, candidate in enumerate(candidates):
            candidate.candidate_id = candidate.candidate_id or f"{analysis_id}:{index}"
        self.saved[analysis_id] = list(candidates)
        analysis.finish(OperationStatus.COMPLETED, finished_at, stats)
        return True

    def list_candidates(
        self,
        analysis_id: str,
        *,
        include_excluded: bool,
        after: tuple[int, float, int] | None,
        limit: int,
    ) -> tuple[list[Candidate], int]:
        """Порядок как в SQL: слабые сигналы по рангу, затем исключённые по убыванию оценки."""
        rows = [
            candidate
            for candidate in self.saved.get(analysis_id, [])
            if include_excluded or candidate.decision is Decision.WEAK_SIGNAL
        ]
        ordered = sorted(rows, key=_sort_key)
        if after is not None:
            ordered = [item for item in ordered if _sort_key(item) > after]
        return ordered[:limit], len(rows)


def _sort_key(candidate: Candidate) -> tuple[int, float, int]:
    """Ключ сортировки кандидатов (совпадает с выражением CTE в репозитории)."""
    group = 0 if candidate.decision is Decision.WEAK_SIGNAL else 1
    order = float(candidate.rank) if group == 0 else -candidate.score
    return (group, order, candidate.cluster_index)


class InMemoryEmbeddingCache:
    """Реализация `EmbeddingCache` в памяти."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], CachedEmbedding] = {}
        self.writes: int = 0

    def get_many(self, document_ids: Sequence[str], embedding_model: str) -> dict[str, CachedEmbedding]:
        """Записи кеша по документам."""
        return {
            document_id: self.entries[(document_id, embedding_model)]
            for document_id in document_ids
            if (document_id, embedding_model) in self.entries
        }

    def put_many(self, entries: Sequence[CachedEmbedding], embedding_model: str, dims: int) -> None:
        """Сохраняет записи."""
        self.writes += 1
        for entry in entries:
            self.entries[(entry.document_id, embedding_model)] = entry


class InMemoryModelVersionRepository:
    """Реализация `ModelVersionRepository` в памяти с инвариантом «одна активная»."""

    def __init__(self) -> None:
        self.versions: dict[str, ModelVersion] = {}

    def activate(self, version: ModelVersion) -> None:
        """Делает версию единственной активной."""
        for key, existing in self.versions.items():
            self.versions[key] = replace(existing, is_active=False)
        self.versions[version.model_version_id] = version

    def get_active(self) -> ModelVersion | None:
        """Активная версия."""
        return next((item for item in self.versions.values() if item.is_active), None)


def make_document(
    index: int,
    title: str,
    text: str = "",
    source_type: SourceType = SourceType.SCIENTIFIC_PUBLICATION,
    trust_level: TrustLevel = TrustLevel.HIGH,
    published_at: datetime | None = None,
    citation_count: int | None = None,
) -> DocumentRef:
    """Документ с предсказуемым UUID для тестов."""
    return DocumentRef(
        document_id=f"{index:08d}-0000-4000-8000-000000000000",
        title=title,
        text=text or title,
        language_code="ru",
        source_type=source_type,
        trust_level=trust_level,
        origin_domain="example.com",
        url=f"https://example.com/{index}",
        published_at=published_at,
        citation_count=citation_count,
        relevance_rank=index,
    )
