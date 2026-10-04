"""Адаптеры повтора режима А: хранилища в памяти, записанные внешние границы и транспорт без сети.

Внешние границы отвечают тем, что записал живой конвейер: эмбеддинги — векторами записи анализа, Wikipedia —
ответами записи, LLM — записанными ответами по ключу запроса. Межсервисные вызовы идут через те же функции
преобразования, что и gRPC (`to_proto_*`, `mappers.*`), с сериализацией сообщения туда и обратно, — так повтор видит
те же округления float32, что и живой конвейер. Отсутствие записи — явная ошибка с подсчётом, а не тихая подмена.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections import defaultdict, deque
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from analyzer.adapters.inbound.mappers import to_proto_candidate
from analyzer.adapters.outbound.collector_mappers import to_document_ref
from analyzer.application.capture import TextCall, hit_from_dict
from analyzer.application.dto import CachedEmbedding, EncyclopediaHit, LeaseState
from analyzer.domain.errors import CollectorUnavailable
from collector.adapters.inbound.mappers import to_proto_document
from collector.adapters.outbound.postgres.mappers import row_to_document
from insight.adapters.inbound import mappers as insight_mappers
from insight.adapters.inbound import rpc as insight_rpc
from insight.application.dto import LLMResult
from insight.application.llm_recording import replay_key
from insight.domain.errors import ProviderBlocked, ProviderError, ProviderRateLimited, ProviderTimeout
from ml.benchmark.snapshot import parse_time
from orchestrator.adapters.outbound.grpc import mappers as orchestrator_mappers
from orchestrator.domain.errors import UpstreamUnavailable


class ReplayError(RuntimeError):
    """Повтор не может продолжаться: запрос вне записанного объёма."""


def wire(message: Any) -> Any:
    """Сообщение protobuf после сериализации туда и обратно — как после передачи по сети."""
    return type(message).FromString(message.SerializeToString())


@dataclasses.dataclass
class Misses:
    """Обращения, на которые в записи нет ответа: повтор с ними неполный."""

    llm: list[dict[str, str]] = dataclasses.field(default_factory=list)
    embeddings: list[str] = dataclasses.field(default_factory=list)
    encyclopedia: list[str] = dataclasses.field(default_factory=list)
    embedding_lookups: int = 0  # вызовы, найденные не по порядку, а поиском текста (повтор с изменённым отбором)

    def total(self) -> int:
        """Число обращений без записи."""
        return len(self.llm) + len(self.embeddings) + len(self.encyclopedia)


# ----------------------------- документы collector -----------------------------


def document_from_row(row: dict[str, Any]):  # noqa: ANN201 - collector Document
    """Строка `documents` из снимка → документ collector (как его читает репозиторий)."""
    values = dict(row)
    values["published_at"] = parse_time(values.get("published_at"))
    values["fetched_at"] = parse_time(values.get("fetched_at")) or datetime(1970, 1, 1, tzinfo=UTC)
    return row_to_document(values)


def ordered_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Порядок документов коллекции, как в StreamDocuments: (relevance_rank, document_id)."""
    return sorted(rows, key=lambda row: (int(row["relevance_rank"]), str(row["document_id"])))


# ----------------------------- analyzer -----------------------------


class FrozenSyncClock:
    """Часы analyzer на дату записи: признаки свежести считаются на тот же день, что в живом анализе."""

    def __init__(self, as_of: datetime) -> None:
        self._as_of = as_of
        self._tick = itertools.count()

    def now(self) -> datetime:
        return self._as_of

    def monotonic(self) -> float:
        return float(next(self._tick))

    def sleep(self, seconds: float) -> None:
        return None


class MemoryAnalysisRepository:
    """Анализ в памяти: аренда всегда жива, отмены нет; итог отказа сохраняется для отчёта."""

    def __init__(self) -> None:
        self.finished: dict[str, Any] = {}

    def heartbeat(self, analysis_id: str, owner: str, lease_seconds: int) -> LeaseState:
        return LeaseState(alive=True, cancel_requested=False)

    def finish(self, analysis_id: str, owner: str, status, *, stats, error_code: str, error_message: str,  # noqa: ANN001
               finished_at: datetime) -> bool:
        self.finished = {"status": getattr(status, "value", str(status)), "error_code": error_code,
                         "error_message": error_message}
        return True


class MemoryCandidateRepository:
    """Кандидаты в памяти; порядок выдачи — как у запроса PostgreSQL (слабые по рангу, затем по убыванию скора)."""

    def __init__(self) -> None:
        self.candidates: list[Any] = []
        self.stats: Any = None

    def save_results(self, analysis_id: str, owner: str, candidates: Sequence[Any], stats, finished_at) -> bool:  # noqa: ANN001
        self.candidates = list(candidates)
        self.stats = stats
        return True

    def assign_ids(self, live: Sequence[dict[str, Any]], namespace: str) -> dict[str, str]:
        """Идентификаторы кандидатов как у живого анализа: тот же кластер с тем же составом документов.

        Новые кластеры (повтор с изменённым отбором) получают детерминированный идентификатор UUID5.
        """
        import uuid  # noqa: PLC0415

        by_cluster = {int(row["cluster_index"]): row for row in live}
        report: dict[str, str] = {}
        result = []
        for candidate in self.candidates:
            documents = sorted(document.document_id for document in candidate.documents)
            known = by_cluster.get(candidate.cluster_index)
            if known is not None and sorted(known.get("document_ids") or []) == documents:
                candidate_id = str(known["candidate_id"])
                report[candidate_id] = "live"
            else:
                key = f"{namespace}:{candidate.cluster_index}:{','.join(documents)}"
                candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
                report[candidate_id] = "new"
            result.append(dataclasses.replace(candidate, candidate_id=candidate_id))
        self.candidates = result
        return report

    def ordered(self) -> list[Any]:
        weak = sorted((c for c in self.candidates if c.decision.value == "WEAK_SIGNAL"), key=lambda c: c.rank)
        rest = sorted((c for c in self.candidates if c.decision.value != "WEAK_SIGNAL"),
                      key=lambda c: (-float(c.score), c.cluster_index))
        return weak + rest


class MemoryEmbeddingCache:
    """Кеш эмбеддингов документов, заполненный векторами записи (те, что реально видел живой анализ)."""

    def __init__(self, documents: dict[str, tuple[str, np.ndarray]]) -> None:
        self._documents = documents

    def get_many(self, document_ids: Sequence[str], embedding_model: str) -> dict[str, CachedEmbedding]:
        return {doc_id: CachedEmbedding(doc_id, self._documents[doc_id][0], self._documents[doc_id][1])
                for doc_id in document_ids if doc_id in self._documents}

    def put_many(self, entries: Sequence[CachedEmbedding], embedding_model: str, dims: int) -> None:
        return None


class ReplayEmbedder:
    """Эмбеддер записи: вызовы в том же порядке получают те же векторы; иначе — поиск текста в записи."""

    def __init__(self, model_name: str, calls: Sequence[TextCall], misses: Misses) -> None:
        self._model_name = model_name
        self._calls = deque(calls)
        self._lookup: dict[tuple[str, str], np.ndarray] = {}
        for call in calls:
            for text, vector in zip(call.texts, call.vectors, strict=True):
                self._lookup.setdefault((call.prefix, text), vector)
        dims = {int(call.vectors.shape[1]) for call in calls if call.vectors.ndim == 2 and call.vectors.shape[0]}
        self._dims = dims.pop() if len(dims) == 1 else 0
        self._misses = misses

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dims(self) -> int:
        return self._dims

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        texts = list(texts)
        if self._calls and self._calls[0].prefix == prefix and list(self._calls[0].texts) == texts:
            return np.array(self._calls.popleft().vectors, copy=True)
        rows = []
        for text in texts:
            vector = self._lookup.get((prefix, text))
            if vector is None:
                self._misses.embeddings.append(f"{prefix}{text[:80]}")
                raise ReplayError(f"нет записанного эмбеддинга: {prefix}{text[:80]!r}")
            rows.append(vector)
        self._misses.embedding_lookups += 1
        return np.vstack(rows)


class SnapshotCollectorReader:
    """Порт collector для analyzer: документы снимка и записанные ответы Wikipedia."""

    def __init__(self, rows: Sequence[dict[str, Any]], encyclopedia: Sequence[dict[str, Any]], misses: Misses) -> None:
        self._rows = ordered_rows(rows)
        self._encyclopedia: dict[tuple[str, tuple[str, ...]], deque[dict[str, Any]]] = defaultdict(deque)
        for entry in encyclopedia:
            self._encyclopedia[(entry["language"], tuple(entry["titles"]))].append(entry)
        self._misses = misses

    def get_collection(self, collection_id: str):  # noqa: ANN201
        raise ReplayError("повтор не запрашивает состояние коллекции")

    def stream_documents(self, collection_id: str, chunk_size: int, limit: int) -> Iterator[Any]:
        """Документы в порядке StreamDocuments; номер — как у клиента gRPC (счётчик с нуля)."""
        for produced, row in enumerate(self._rows):
            if produced >= limit:
                return
            yield to_document_ref(wire(to_proto_document(document_from_row(row))), produced)

    def check_encyclopedia(self, titles: Sequence[str], language_code: str) -> list[EncyclopediaHit]:
        queue = self._encyclopedia.get((language_code, tuple(titles)))
        if not queue:
            self._misses.encyclopedia.append(f"{language_code}:{'|'.join(titles)[:120]}")
            raise CollectorUnavailable("повтор: нет записанного ответа энциклопедии")
        entry = queue.popleft()
        if "error" in entry:
            raise CollectorUnavailable(str(entry["error"]))
        return [hit_from_dict(hit) for hit in entry.get("hits") or []]

    def start_enrichment(self, idempotency_key: str, title: str) -> str:
        raise ReplayError("повтор не запускает сбор")


def feature_labels(registry: Any, registry_path: str) -> dict[str, str]:
    """Подписи признаков, как их подставляет репозиторий кандидатов analyzer при чтении (v1 и v2)."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    labels = {spec.name: spec.label_ru for spec in registry.features}
    v2_path = Path(registry_path).with_name("feature_registry_v2.json")
    if v2_path.is_file():
        for item in json.loads(v2_path.read_text(encoding="utf-8")).get("features", []):
            labels.setdefault(item["name"], item["label_ru"])
    return labels


class LoopbackAnalyzer:
    """Порт AnalyzerClient orchestrator поверх кандидатов повтора (через те же преобразования, что gRPC)."""

    def __init__(self, repository: MemoryCandidateRepository, labels: dict[str, str]) -> None:
        self._repository = repository
        self._labels = labels

    async def list_candidates(self, analysis_id: str, *, include_excluded: bool, page_size: int, page_token: str):  # noqa: ANN201
        views = []
        for candidate in self._repository.ordered():
            if not include_excluded and candidate.decision.value != "WEAK_SIGNAL":
                continue
            features = tuple(dataclasses.replace(f, label_ru=self._labels.get(f.feature_name, f.label_ru))
                             for f in candidate.features)
            proto = wire(to_proto_candidate(dataclasses.replace(candidate, features=features), analysis_id))
            views.append(orchestrator_mappers.to_candidate_view(proto))
        return views, ""


class LoopbackCollector:
    """Порт CollectorClient orchestrator: документы снимка по идентификаторам."""

    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        self._rows = {str(row["document_id"]): row for row in rows}

    async def get_documents(self, document_ids: Sequence[str]):  # noqa: ANN201
        return [orchestrator_mappers.to_document_view(wire(to_proto_document(document_from_row(self._rows[d]))))
                for d in document_ids if d in self._rows]


# ----------------------------- insight -----------------------------


class ReplayClock:
    """Асинхронные часы повтора: паузы цепочки LLM (ожидание после 429) не ждут реального времени."""

    def __init__(self, as_of: datetime) -> None:
        self._as_of = as_of
        self._now = 0.0

    def now(self) -> datetime:
        return self._as_of

    def monotonic(self) -> float:
        self._now += 0.001
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += max(0.0, seconds)


_ERRORS = {"TIMEOUT": ProviderTimeout, "RATE_LIMITED": ProviderRateLimited, "BLOCKED": ProviderBlocked}


class ReplayProvider:
    """Провайдер LLM записи: ответы по ключу запроса в порядке записи; нет записи — ошибка с подсчётом."""

    def __init__(self, name: str, model: str, recordings: Sequence[dict[str, Any]], misses: Misses) -> None:
        self._name = name
        self._model = model
        self._queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for record in recordings:
            if record["provider"] == name:
                self._queues[record["replay_key"]].append(record)
        self._misses = misses

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_concurrency(self) -> int:
        return 1

    async def complete(self, messages, *, json_schema, max_tokens: int, temperature: float,  # noqa: ANN001
                       deadline_seconds: float) -> LLMResult:
        key = replay_key(messages, json_schema, max_tokens, temperature)
        queue = self._queues.get(key)
        if not queue:
            self._misses.llm.append({"provider": self._name, "replay_key": key})
            raise ProviderError("повтор: нет записанного ответа на этот запрос", self._name)
        record = queue.popleft()
        status = str(record.get("status") or "")
        if status != "OK":
            error_type = _ERRORS.get(status, ProviderError)
            message = str(record.get("error_message") or status)
            if error_type is ProviderRateLimited:
                raise ProviderRateLimited(message, self._name, float(record.get("retry_after_seconds") or 5.0))
            raise error_type(message, self._name)
        return LLMResult(text=str(record.get("response_text") or ""), model=str(record.get("model") or self._model),
                         prompt_tokens=int(record.get("prompt_tokens") or 0),
                         completion_tokens=int(record.get("completion_tokens") or 0),
                         latency_ms=int(record.get("latency_ms") or 0))

    async def healthcheck(self) -> bool:
        return True


class MemoryCallLog:
    """Журнал вызовов LLM в памяти (суточный бюджет при повторе не применяется)."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def record(self, entry: Any) -> None:
        self.records.append(entry)

    async def tokens_used_today(self, now: datetime) -> int:
        return 0


class MemoryInsightRepository:
    """Хранилище инсайтов без кеша: повтор не подставляет ответы, которых не было в записи."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    async def get_by_idempotency_key(self, key: str) -> Any:
        return self.saved.get(key)

    async def get_by_input_hash(self, input_hash: str, not_older_than: datetime) -> Any:
        return None

    async def save(self, insight: Any) -> Any:
        self.saved[getattr(insight, "idempotency_key", str(len(self.saved)))] = insight
        return insight


class LoopbackInsight:
    """Порт InsightClient orchestrator: те же запросы и разбор ответов, что у gRPC, сценарии insight в процессе."""

    def __init__(self, judge_candidates: Any, rubric_judge: Any, finalize_cards: Any, generate_insight: Any) -> None:
        self._judge = judge_candidates
        self._rubric = rubric_judge
        self._finalize = finalize_cards
        self._generate = generate_insight

    async def expand_query(self, query_text: str):  # noqa: ANN201
        raise ReplayError("расширение запроса не повторяется: фразы уже в запросе анализа снимка")

    async def judge_candidates(self, query_text: str, candidates):  # noqa: ANN001, ANN201
        request = wire(orchestrator_mappers.judge_request(query_text, candidates))
        return orchestrator_mappers.judge_verdicts(wire(await _call(insight_rpc.judge(request, self._judge, self._rubric))))

    async def judge_rubric(self, query_text: str, items, final_check: bool = False):  # noqa: ANN001, ANN201
        request = wire(orchestrator_mappers.rubric_request(query_text, items, final_check))
        return orchestrator_mappers.rubric_verdicts(wire(await _call(insight_rpc.judge(request, self._judge, self._rubric))))

    async def finalize_cards(self, query_text: str, cards):  # noqa: ANN001, ANN201
        request = wire(orchestrator_mappers.finalize_request(query_text, cards))
        return orchestrator_mappers.finalized_cards(wire(await _call(insight_rpc.finalize(request, self._finalize))))

    async def generate_insight(self, idempotency_key: str, candidate, query_text: str, evidence, prompt_version: str):  # noqa: ANN001, ANN201
        request = wire(orchestrator_mappers.generate_request(idempotency_key, candidate, query_text, evidence))
        command = insight_mappers.from_generate_request(request)
        insight = await _call(self._generate.execute(command))
        return orchestrator_mappers.narrative_view(wire(insight_mappers.to_generate_response(insight)), prompt_version)


async def _call(awaitable: Any) -> Any:
    """Прикладная ошибка insight → недоступность для orchestrator (как статус gRPC у живого клиента)."""
    from insight.domain.errors import AppError  # noqa: PLC0415

    try:
        return await awaitable
    except AppError as error:
        raise UpstreamUnavailable(f"insight: {error.error_code}: {error.message}") from error


class MemoryResultRepository:
    """Результат задания в памяти: элементы выдачи, исключённые, статистика и трасса."""

    def __init__(self) -> None:
        self.items: list[Any] = []
        self.excluded: list[Any] = []
        self.stats: Any = None
        self.traces: dict[str, dict[str, Any]] = {}

    async def add_item(self, item: Any) -> str:
        self.items.append(item)
        return str(len(self.items))

    async def add_excluded(self, job_id: str, batch: Sequence[Any]) -> int:
        self.excluded.extend(batch)
        return len(batch)

    async def set_stats(self, job_id: str, stats: Any) -> None:
        self.stats = stats

    async def get_stats(self, job_id: str) -> Any:
        return self.stats

    async def count_items(self, job_id: str) -> int:
        return len(self.items)

    async def save_trace(self, job_id: str, stage: str, payload: dict[str, Any]) -> None:
        self.traces[stage] = payload
