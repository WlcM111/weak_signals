"""Сценарий RunAnalysis: конвейер инференса от документов коллекции до кандидатов (§12.5 ТЗ).

Шаги: загрузка документов → эмбеддинги с кешем → near-dup → фильтр релевантности запросу →
кластеризация → ключевые фразы → 25 признаков → правила исключения → скоринг модели →
ранжирование → доказательства → транзакционная запись результата.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np

from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.dto import CachedEmbedding, EncyclopediaHit, ModelBundle
from analyzer.application.ports import (
    AnalysisRepository,
    CandidateRepository,
    CollectorReader,
    Embedder,
    EmbeddingCache,
    MetricsSink,
    NullMetrics,
)
from analyzer.application.scoring import build_contributions, merge_feature_values, score_rows
from analyzer.domain.clustering import (
    centroid,
    cluster_documents,
    near_dup_filter,
    normalize_rows,
    select_top_clusters,
    split_market_glue,
    split_small_clusters,
    trust_weights,
)
from analyzer.domain.entities import Candidate, ClusterDocument, DocumentRef
from analyzer.domain.errors import CollectorUnavailable, LeaseLost
from analyzer.domain.evidence import build_snippet, select_evidence
from analyzer.domain.features_v2 import EvidenceItem, ObservationVectors, observation_features
from analyzer.domain.features import (
    EncyclopediaSignal,
    Lexicons,
    collection_features,
    cosine,
    embedding_features,
    encyclopedia_features,
    lexical_features,
)
from analyzer.domain.keyphrases import candidate_phrases, fallback_title, select_by_mmr
from analyzer.domain.rules import (
    apply_exclusion_rules,
    decide_by_score,
    explain_model_decision,
    rank_weak_signals,
)
from analyzer.domain.values import (
    CONFIDENT_SCORE,
    AnalysisErrorCode,
    AnalysisStats,
    Decision,
    DecisionReason,
    OperationStatus,
    SourceType,
)
from ws_common.clock import SyncClock
from ws_common.logging import get_logger

PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "
MAX_ENCYCLOPEDIA_TITLES = 20
ENCYCLOPEDIA_TITLES_PER_CANDIDATE = 2
ENCYCLOPEDIA_MIN_WORDS = 2
MAX_ENCYCLOPEDIA_FAILURES = 2


# Типы одиночных документов, которые остаются кандидатами при keep_market_singletons (рыночные сигналы).
MARKET_SINGLE_TYPES = frozenset({SourceType.INDUSTRY_MEDIA, SourceType.NEWS})


def phrase_relevance(rows: np.ndarray, mean_vector: np.ndarray, phrases: np.ndarray | None, mode: str) -> np.ndarray:
    """Косинусная близость строк (нормированных) к запросу: к среднему вектору или к ближайшей фразе."""
    if mode == "max" and phrases is not None and len(phrases):
        return (rows @ phrases.T).max(axis=1)
    return rows @ mean_vector


def _types(documents: Sequence) -> dict[str, int]:
    """Число документов по типам источников — для журнала воронки анализа."""
    return dict(Counter(document.source_type.value for document in documents))


@dataclass(frozen=True, slots=True)
class RunAnalysisConfig:
    """Параметры конвейера анализа (§12 HANDOFF)."""

    lease_seconds: int = 60
    heartbeat_seconds: int = 20
    max_documents: int = 3000
    stream_chunk_size: int = 200
    embedding_batch_size: int = 32
    min_doc_query_sim: float = 0.25
    min_candidate_query_sim: float = 0.0
    trust_weighted_selection: bool = False
    require_high_trust_source: bool = False
    near_dup_threshold: float = 0.92
    cluster_distance_threshold: float = 0.35
    min_cluster_size: int = 2
    # Одиночные документы отраслевых СМИ и новостей остаются кандидатами (рыночные сигналы, режим rubric).
    keep_market_singletons: bool = False
    # Близость к запросу: mean — к среднему вектору запроса и его расширений (прежнее поведение); max — к ближайшей
    # фразе (исходный запрос или любое поднаправление). С поднаправлениями среднее размывается: 26–27.09 исключений
    # LOW_QUERY_RELEVANCE стало 58 против 33, и ниши, ради которых шёл поиск, отсекались как «не по теме».
    query_relevance_mode: str = "mean"
    # Заметки СМИ не склеиваются между собой: в кластере остаётся одна (ближайшая к центру), остальные — отдельные
    # кандидаты. 27.09 новости о раундах разных компаний (PicoJool, BigHat, Feather) склеились по словам «raises Series».
    market_notes_separate: bool = False
    # Сколько из max_candidates мест зарезервировать под кластеры с документами СМИ (0 — без резерва).
    market_reserved_candidates: int = 0
    evidence_max: int = 8
    keyphrases_top_k: int = 10
    encyclopedia_languages: tuple[str, ...] = ("ru", "en")
    # Абляция вклада модели в живых прогонах: none | constant | shuffle (только для экспериментов).
    ml_score_ablation: str = "none"


class _CancelRequested(Exception):
    """Внутренний сигнал кооперативной отмены между шагами конвейера."""


@dataclass(slots=True)
class _ClusterView:
    """Промежуточное состояние кандидата на время выполнения конвейера."""

    cluster_index: int
    documents: list[DocumentRef]
    similarities: list[float]
    centroid: np.ndarray
    title_auto: str
    keyphrases: tuple[str, ...]
    query_relevance: float
    values: dict[str, float] = field(default_factory=dict)


class RunAnalysis:
    """Выполняет анализ, захваченный воркером, и записывает кандидатов."""

    def __init__(
        self,
        analyses: AnalysisRepository,
        candidates: CandidateRepository,
        embedding_cache: EmbeddingCache,
        collector: CollectorReader,
        embedder: Embedder,
        active_model: ActiveModelHolder,
        lexicons: Lexicons,
        clock: SyncClock,
        config: RunAnalysisConfig,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._analyses = analyses
        self._candidates = candidates
        self._embedding_cache = embedding_cache
        self._collector = collector
        self._embedder = embedder
        self._active_model = active_model
        self._lexicons = lexicons
        self._clock = clock
        self._config = config
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._log = get_logger("analyzer.run_analysis")

    def execute(self, analysis, owner: str) -> OperationStatus:  # noqa: ANN001 - сущность Analysis
        """Проводит анализ до терминального статуса и возвращает его."""
        started = self._clock.monotonic()
        bundle = self._active_model.get()
        control = _LeaseControl(self._analyses, analysis.analysis_id, owner, self._clock, self._config)
        stats = AnalysisStats()
        self._log.info(
            "analysis.started",
            analysis_id=analysis.analysis_id,
            collection_id=analysis.collection_id,
            model_version_id=analysis.model_version_id,
        )
        try:
            stats = self._pipeline(analysis, owner, bundle, control, started)
        except _CancelRequested:
            return self._fail(
                analysis, owner, OperationStatus.CANCELLED, AnalysisErrorCode.CANCELLED,
                "анализ отменён по запросу", stats, started,
            )
        except LeaseLost:
            self._log.warning("analysis.lease_lost", analysis_id=analysis.analysis_id)
            raise
        except CollectorUnavailable as error:
            return self._fail(
                analysis, owner, OperationStatus.FAILED, AnalysisErrorCode.COLLECTOR_UNAVAILABLE,
                error.message, stats, started,
            )
        except _NoDocuments as error:
            return self._fail(
                analysis, owner, OperationStatus.FAILED, AnalysisErrorCode.NO_DOCUMENTS,
                str(error), stats, started,
            )
        except _EmbeddingFailed as error:
            return self._fail(
                analysis, owner, OperationStatus.FAILED, AnalysisErrorCode.EMBEDDING_FAILED,
                str(error), stats, started,
            )
        except _ModelFailed as error:
            return self._fail(
                analysis, owner, OperationStatus.FAILED, AnalysisErrorCode.MODEL_ERROR,
                str(error), stats, started,
            )
        self._metrics.analysis_finished(OperationStatus.COMPLETED)
        self._log.info(
            "analysis.finished",
            analysis_id=analysis.analysis_id,
            status=OperationStatus.COMPLETED.value,
            documents_input=stats.documents_input,
            clusters_total=stats.clusters_total,
            weak_signals_total=stats.weak_signals_total,
            duration_ms=stats.duration_ms,
        )
        return OperationStatus.COMPLETED

    def _pipeline(
        self, analysis, owner: str, bundle: ModelBundle, control: _LeaseControl, started: float
    ) -> AnalysisStats:  # noqa: ANN001 - сущность Analysis
        """Последовательность шагов конвейера; между шагами проверяются аренда и отмена."""
        with self._step("stream_documents"):
            documents = self._load_documents(analysis.collection_id)
        documents_input = len(documents)
        if not documents:
            raise _NoDocuments("коллекция не вернула документов")
        control.check()

        with self._step("embeddings"):
            vectors = self._embed_documents(documents, control)
        control.check()

        with self._step("near_dup"):
            kept, dropped = near_dup_filter(documents, vectors, self._config.near_dup_threshold)
            documents = [documents[index] for index in kept]
            vectors = vectors[kept]
        documents_after_dedup = len(documents)
        self._log.info(
            "analysis.step", step="near_dup", kept=documents_after_dedup, dropped=len(dropped)
        )
        control.check()

        with self._step("query_relevance"):
            query_vector, query_phrases = self._embed_query(analysis.query_text)
            relevances = phrase_relevance(normalize_rows(vectors), query_vector, query_phrases,
                                          self._config.query_relevance_mode)
            relevant = [
                index
                for index, value in enumerate(relevances)
                if float(value) >= self._config.min_doc_query_sim
            ]
            relevant_set = set(relevant)
            dropped_types = Counter(documents[index].source_type.value for index in range(len(documents))
                                    if index not in relevant_set)
            documents = [documents[index] for index in relevant]
            vectors = vectors[relevant]
            relevances = relevances[relevant]
        self._log.info("analysis.funnel", step="query_relevance", kept=_types(documents),
                       dropped=dict(dropped_types))
        if not documents:
            raise _NoDocuments("после фильтра релевантности запросу не осталось документов")
        control.check()

        with self._step("clustering"):
            clusters = cluster_documents(vectors, self._config.cluster_distance_threshold)
            if self._config.market_notes_separate:
                clusters = split_market_glue(clusters, documents, vectors, MARKET_SINGLE_TYPES)
            clusters_total = len(clusters)
            scored_clusters, misc = split_small_clusters(
                clusters, documents, self._config.min_cluster_size,
                MARKET_SINGLE_TYPES if self._config.keep_market_singletons else frozenset(),
            )
            self._log.info("analysis.funnel", step="clustering",
                           kept=_types([documents[i] for cluster in scored_clusters for i in cluster]),
                           misc=_types([documents[i] for i in misc]))
            scored_clusters = select_top_clusters(
                scored_clusters,
                [float(value) for value in relevances],
                analysis.params.max_candidates,
                trust_weights(documents) if self._config.trust_weighted_selection else None,
                [any(documents[i].source_type in MARKET_SINGLE_TYPES for i in cluster) for cluster in scored_clusters],
                self._config.market_reserved_candidates,
            )
        self._log.info(
            "analysis.step",
            step="clustering",
            clusters_total=clusters_total,
            scored=len(scored_clusters),
            misc_documents=len(misc),
        )
        control.check()

        with self._step("keyphrases"):
            views = []
            for index, cluster in enumerate(scored_clusters):
                views.append(self._build_view(index, cluster, documents, vectors, query_vector, query_phrases))
                control.check()  # эмбеддинги фраз десятков кластеров на CPU идут дольше аренды
        control.check()

        with self._step("features"):
            signals = self._encyclopedia_signals(views, control)
            now = self._clock.now()
            for view in views:
                view.values = self._feature_values(view, signals.get(view.cluster_index), bundle, now)
            if bundle.feature_schema == "v2":
                self._add_v2_features(views, analysis.query_text, bundle, now.year)
        control.check()

        with self._step("scoring"):
            candidates = self._decide(views, bundle, analysis, control)
        control.check()

        ranked = rank_weak_signals(candidates)
        stats = self._build_stats(
            documents_input=documents_input,
            documents_after_dedup=documents_after_dedup,
            clusters_total=clusters_total,
            candidates=candidates,
            ranked=ranked,
            started=started,
        )
        with self._step("persist"):
            saved = self._candidates.save_results(
                analysis.analysis_id, owner, candidates, stats, self._clock.now()
            )
        if not saved:
            raise LeaseLost(f"аренда анализа {analysis.analysis_id} потеряна при записи результата")
        return stats

    def _load_documents(self, collection_id: str) -> list[DocumentRef]:
        """Читает документы коллекции в память с учётом лимита (§12.5, шаг 1)."""
        documents: list[DocumentRef] = []
        for document in self._collector.stream_documents(
            collection_id, self._config.stream_chunk_size, self._config.max_documents
        ):
            documents.append(document)
            if len(documents) >= self._config.max_documents:
                self._log.warning(
                    "analysis.truncated", collection_id=collection_id, limit=self._config.max_documents
                )
                break
        return documents

    def _embed_documents(self, documents: Sequence[DocumentRef], control: _LeaseControl) -> np.ndarray:
        """Эмбеддинги документов с кешем и батчами; кеш инвалидируется по содержимому."""
        model = self._embedder.model_name
        cached = self._embedding_cache.get_many([document.document_id for document in documents], model)
        vectors: list[np.ndarray | None] = []
        missing: list[int] = []
        hits = 0
        for index, document in enumerate(documents):
            entry = cached.get(document.document_id)
            if entry is not None and entry.content_hash == document.content_key():
                vectors.append(np.asarray(entry.vector, dtype=np.float32))
                hits += 1
            else:
                vectors.append(None)
                missing.append(index)
        self._metrics.embedding_cache_hits(hits)
        fresh: list[CachedEmbedding] = []
        for start in range(0, len(missing), self._config.embedding_batch_size):
            batch = missing[start : start + self._config.embedding_batch_size]
            texts = [documents[index].embedding_text for index in batch]  # префикс добавляет адаптер
            try:
                encoded = np.asarray(self._embedder.encode(texts, PASSAGE_PREFIX), dtype=np.float32)
            except Exception as error:  # noqa: BLE001 - отказ эмбеддера завершает анализ кодом
                raise _EmbeddingFailed(f"не удалось вычислить эмбеддинги: {error}") from error
            for position, index in enumerate(batch):
                vectors[index] = encoded[position]
                fresh.append(
                    CachedEmbedding(
                        document_id=documents[index].document_id,
                        content_hash=documents[index].content_key(),
                        vector=encoded[position],
                    )
                )
            control.check()
        if fresh:
            self._embedding_cache.put_many(fresh, model, self._embedder.dims)
            self._metrics.embeddings_computed(len(fresh))
        self._log.info("analysis.step", step="embeddings", computed=len(fresh), cache_hits=hits)
        return np.vstack([vector for vector in vectors if vector is not None])

    def _embed_query(self, query_text: str) -> tuple[np.ndarray, np.ndarray]:
        """Двуязычный вектор запроса: среднее нормированных векторов запроса и его расширений.

        Orchestrator передаёт в query_text исходную фразу и расширения ExpandQuery через « | ».
        Исходный запрос русский, а профильные документы (GitHub, OpenAlex, arXiv) в основном
        английские; сравнение только с русской фразой ставило русские общие тексты выше
        английских профильных. Строка без разделителя обрабатывается как одна фраза.
        """
        texts = [phrase.strip() for phrase in query_text.split(" | ") if phrase.strip()] or [query_text]
        try:
            encoded = np.asarray(self._embedder.encode(texts, QUERY_PREFIX), dtype=np.float32)
        except Exception as error:  # noqa: BLE001 - без вектора запроса конвейер невозможен
            raise _EmbeddingFailed(f"не удалось вычислить эмбеддинг запроса: {error}") from error
        norms = np.linalg.norm(encoded, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        vector = (encoded / norms).mean(axis=0)
        norm = float(np.linalg.norm(vector))
        self._log.info("analysis.query_vector", phrases=len(texts), mode=self._config.query_relevance_mode)
        return (vector if norm == 0.0 else vector / norm), (encoded / norms).astype(np.float32)

    def _build_view(
        self,
        cluster_index: int,
        cluster: Sequence[int],
        documents: Sequence[DocumentRef],
        vectors: np.ndarray,
        query_vector: np.ndarray,
        query_phrases: np.ndarray | None = None,
    ) -> _ClusterView:
        """Метка кластера, ключевые фразы и близости документов к центроиду."""
        cluster_documents_list = [documents[index] for index in cluster]
        cluster_vectors = normalize_rows(vectors[list(cluster)].astype(np.float32))
        cluster_centroid = centroid(cluster_vectors)
        similarities = [float(max(0.0, min(1.0, value))) for value in cluster_vectors @ cluster_centroid]
        phrases = candidate_phrases(
            [document.title for document in cluster_documents_list], self._lexicons.stopwords
        )
        selected: list[str] = []
        if phrases:
            phrase_vectors = np.asarray(
                self._embedder.encode(list(phrases), QUERY_PREFIX), dtype=np.float32
            )
            selected = select_by_mmr(
                phrases, phrase_vectors, cluster_centroid, self._config.keyphrases_top_k
            )
        title = selected[0] if selected else fallback_title(
            [document.title for document in cluster_documents_list]
        )
        keyphrases = tuple(selected) if selected else (title,)
        return _ClusterView(
            cluster_index=cluster_index,
            documents=cluster_documents_list,
            similarities=similarities,
            centroid=cluster_centroid,
            title_auto=title[:200],
            keyphrases=keyphrases[:10],
            query_relevance=float(max(0.0, min(1.0, float(phrase_relevance(
                cluster_centroid[None, :], query_vector, query_phrases, self._config.query_relevance_mode)[0])))),
        )

    def _encyclopedia_signals(
        self, views: Sequence[_ClusterView], control: _LeaseControl
    ) -> dict[int, EncyclopediaSignal]:
        """Проверяет названия кандидатов в Wikipedia батчами и берёт лучший результат.

        Проверяются только многословные фразы: у одиночного слова («security», «LLM») почти всегда
        есть давняя посещаемая статья, и правило ENCYCLOPEDIA_MATURE ошибочно исключало бы
        кластер о новой технологии из-за общего родового термина в его ключевых фразах.
        """
        requests: list[tuple[int, str]] = []
        for view in views:
            titles = [view.title_auto, *view.keyphrases[:3]]
            specific = [
                title
                for title in dict.fromkeys(title for title in titles if title)
                if len(title.split()) >= ENCYCLOPEDIA_MIN_WORDS
            ]
            for title in specific[:ENCYCLOPEDIA_TITLES_PER_CANDIDATE]:
                requests.append((view.cluster_index, title))
        best: dict[int, EncyclopediaSignal] = {}
        failures = 0
        for language in self._config.encyclopedia_languages:
            for start in range(0, len(requests), MAX_ENCYCLOPEDIA_TITLES):
                batch = requests[start : start + MAX_ENCYCLOPEDIA_TITLES]
                control.check()
                try:
                    hits = self._collector.check_encyclopedia([title for _, title in batch], language)
                except CollectorUnavailable as error:
                    # отсутствие индикатора зрелости не должно ронять анализ целиком; один сбой
                    # пачки не отменяет остальные, два подряд — прекращают проверку
                    failures += 1
                    self._log.warning("analysis.encyclopedia_unavailable", error=str(error))
                    if failures >= MAX_ENCYCLOPEDIA_FAILURES:
                        return best
                    continue
                failures = 0
                for (cluster_index, _), hit in zip(batch, hits, strict=False):
                    best[cluster_index] = _better_signal(best.get(cluster_index), hit)
        return best

    def _feature_values(
        self,
        view: _ClusterView,
        signal: EncyclopediaSignal | None,
        bundle: ModelBundle,
        now: datetime,
    ) -> dict[str, float]:
        """25 признаков реестра для кандидата."""
        text = " ".join(
            f"{document.title}. {document.text}" for document in view.documents
        )
        return merge_feature_values(
            lexical_features(text, self._lexicons, now),
            collection_features(view.documents, now),
            encyclopedia_features(signal or EncyclopediaSignal(), now),
            embedding_features(
                view.centroid, bundle.weak_centroid, bundle.mature_centroid, None
            )
            | {"emb_sim_query": view.query_relevance},
        )

    def _add_v2_features(
        self, views: Sequence[_ClusterView], query_text: str, bundle: ModelBundle, as_of_year: int
    ) -> None:
        """Признаки v2 по теме, названию кандидата и названиям его доказательств (тот же код, что в ml)."""
        spec = bundle.v2
        topic = query_text.split(" | ")[0].strip()
        topic_vector = self._encode_batched([topic], spec.query_prefix)[0] if topic else None
        plans: list[tuple[_ClusterView, list[DocumentRef], int]] = []
        texts: list[str] = []
        for view in views:
            indexes = select_evidence(view.documents, view.similarities, self._config.evidence_max)
            documents = [view.documents[index] for index in indexes]
            plans.append((view, documents, len(texts)))
            texts.extend([view.title_auto, *[document.title for document in documents]])
        vectors = self._encode_batched(texts, spec.passage_prefix) if texts else np.zeros((0, 1))
        for view, documents, start in plans:
            evidence = [
                EvidenceItem(doc.title, doc.source_type.value, doc.trust_level.value, doc.published_year)
                for doc in documents
            ]
            observed = ObservationVectors(topic_vector, vectors[start], vectors[start + 1 : start + 1 + len(documents)])
            view.values = view.values | observation_features(
                topic, view.title_auto, evidence, observed, spec.projection, self._lexicons, as_of_year,
                spec.glossary, cluster_query_similarity=view.query_relevance,
            )

    def _encode_batched(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Эмбеддинги коротких текстов батчами; отказ эмбеддера — код EMBEDDING_FAILED."""
        parts: list[np.ndarray] = []
        size = self._config.embedding_batch_size
        try:
            for start in range(0, len(texts), size):
                parts.append(np.asarray(self._embedder.encode(list(texts[start : start + size]), prefix), dtype=np.float64))
        except Exception as error:  # noqa: BLE001 - без векторов признаки v2 невозможны
            raise _EmbeddingFailed(f"не удалось вычислить эмбеддинги признаков v2: {error}") from error
        return np.vstack(parts)

    def _ablate(self, scores: np.ndarray, key: str, threshold: float) -> np.ndarray:
        """Абляция вклада модели: константа (все равны порогу) или детерминированная перестановка."""
        mode = self._config.ml_score_ablation
        if mode == "none" or scores.size == 0:
            return scores
        self._log.warning("analysis.ml_ablation", mode=mode, analysis_id=key)
        if mode == "constant":
            return np.full_like(scores, max(float(threshold), 1e-6))
        seed = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
        return scores[np.random.default_rng(seed).permutation(scores.size)]

    def _decide(
        self,
        views: Sequence[_ClusterView],
        bundle: ModelBundle,
        analysis,  # noqa: ANN001 - сущность Analysis
        control: _LeaseControl,
    ) -> list[Candidate]:
        """Применяет правила исключения, затем модель, и собирает кандидатов."""
        thresholds = bundle.thresholds
        if self._config.min_candidate_query_sim > 0.0:
            # Порог релевантности задаётся окружением: он зависит от шкалы эмбеддера, а не от модели.
            thresholds = replace(thresholds, min_query_similarity=self._config.min_candidate_query_sim)
        if self._config.require_high_trust_source:
            thresholds = replace(thresholds, require_high_trust=True)
        rule_results = {
            view.cluster_index: apply_exclusion_rules(
                view.values,
                view.documents,
                thresholds,
                analysis.params.min_evidence_documents,
            )
            for view in views
        }
        to_score = [view for view in views if rule_results[view.cluster_index] is None]
        try:
            scores, contributions, stages, trends = score_rows(
                bundle, [view.values for view in to_score]
            )
        except Exception as error:  # noqa: BLE001 - отказ модели завершает анализ кодом MODEL_ERROR
            raise _ModelFailed(f"модель не смогла оценить кандидатов: {error}") from error
        scores = self._ablate(scores, analysis.analysis_id, analysis.params.weak_signal_threshold)
        scored_index = {view.cluster_index: position for position, view in enumerate(to_score)}
        threshold = analysis.params.weak_signal_threshold
        candidates: list[Candidate] = []
        for view in views:
            rule = rule_results[view.cluster_index]
            if rule is not None:
                features = build_contributions(bundle, view.values, None)
                candidate = self._make_candidate(
                    view, 0.0, rule.decision, rule.reason, rule.explanation_ru, features, None, None
                )
            else:
                position = scored_index[view.cluster_index]
                score = float(scores[position])
                features = build_contributions(bundle, view.values, contributions[position])
                decision = decide_by_score(score, threshold, view.values, thresholds)
                candidate = self._make_candidate(
                    view,
                    score,
                    decision,
                    DecisionReason.MODEL_SCORE,
                    explain_model_decision(score, threshold, features),
                    features,
                    stages[position] if position < len(stages) else None,
                    trends[position] if position < len(trends) else None,
                )
            self._metrics.candidate_decided(candidate.decision)
            candidates.append(candidate)
            control.check()
        return candidates

    def _make_candidate(
        self,
        view: _ClusterView,
        score: float,
        decision: Decision,
        reason: DecisionReason,
        explanation: str,
        features,  # noqa: ANN001 - кортеж FeatureContribution
        stage: int | None,
        trend: int | None,
    ) -> Candidate:
        """Собирает кандидата с документами, доказательствами и распределениями."""
        evidence_indexes = set(
            select_evidence(view.documents, view.similarities, self._config.evidence_max)
        )
        documents = tuple(
            ClusterDocument(
                document_id=document.document_id,
                similarity=view.similarities[index],
                is_evidence=index in evidence_indexes,
                snippet=(
                    build_snippet(document.text or document.title, view.keyphrases)
                    if index in evidence_indexes
                    else ""
                ),
                source_type=document.source_type,
                trust_level=document.trust_level,
                published_year=document.published_year,
            )
            for index, document in enumerate(view.documents)
        )
        source_counts: dict = {}
        year_counts: dict = {}
        for document in view.documents:
            source_counts[document.source_type] = source_counts.get(document.source_type, 0) + 1
            if document.published_year is not None:
                year_counts[document.published_year] = year_counts.get(document.published_year, 0) + 1
        return Candidate(
            cluster_index=view.cluster_index,
            title_auto=view.title_auto,
            keyphrases=view.keyphrases,
            score=score,
            decision=decision,
            decision_reason=reason,
            decision_explanation_ru=explanation[:500],
            features=features,
            documents=tuple(sorted(documents, key=lambda item: -item.similarity)),
            document_count=len(view.documents),
            query_relevance=view.query_relevance,
            source_type_counts=source_counts,
            year_counts=year_counts,
            rank=1 if decision is Decision.WEAK_SIGNAL else 0,
            predicted_stage=stage,
            predicted_trend=trend,
        )

    def _build_stats(
        self,
        *,
        documents_input: int,
        documents_after_dedup: int,
        clusters_total: int,
        candidates: Sequence[Candidate],
        ranked: Sequence[Candidate],
        started: float,
    ) -> AnalysisStats:
        """Считает статистику анализа для ответа `GetAnalysis`."""
        by_decision: dict[Decision, int] = {}
        for candidate in candidates:
            by_decision[candidate.decision] = by_decision.get(candidate.decision, 0) + 1
        return AnalysisStats(
            documents_input=documents_input,
            documents_after_dedup=documents_after_dedup,
            clusters_total=clusters_total,
            candidates_scored=len(candidates),
            weak_signals_total=len(ranked),
            weak_signals_confident=sum(1 for item in ranked if item.score >= CONFIDENT_SCORE),
            excluded_mature=by_decision.get(Decision.MATURE, 0),
            excluded_hype_or_noise=by_decision.get(Decision.HYPE_OR_NOISE, 0),
            excluded_insufficient_evidence=by_decision.get(Decision.INSUFFICIENT_EVIDENCE, 0),
            excluded_off_topic=by_decision.get(Decision.OFF_TOPIC, 0),
            duration_ms=int(max(0.0, self._clock.monotonic() - started) * 1000),
        )

    def _fail(
        self,
        analysis,  # noqa: ANN001 - сущность Analysis
        owner: str,
        status: OperationStatus,
        code: AnalysisErrorCode,
        message: str,
        stats: AnalysisStats,
        started: float,
    ) -> OperationStatus:
        """Терминальное завершение без кандидатов."""
        finished_stats = AnalysisStats(
            documents_input=stats.documents_input,
            documents_after_dedup=stats.documents_after_dedup,
            clusters_total=stats.clusters_total,
            candidates_scored=stats.candidates_scored,
            duration_ms=int(max(0.0, self._clock.monotonic() - started) * 1000),
        )
        self._analyses.finish(
            analysis.analysis_id,
            owner,
            status,
            stats=finished_stats,
            error_code=code.value,
            error_message=message,
            finished_at=self._clock.now(),
        )
        self._metrics.analysis_finished(status)
        self._log.warning(
            "analysis.finished",
            analysis_id=analysis.analysis_id,
            status=status.value,
            error_code=code.value,
            error_message=message[:200],
        )
        return status

    @contextmanager
    def _step(self, name: str) -> Iterator[None]:
        """Замер длительности шага конвейера."""
        started = time.perf_counter()
        yield
        duration = time.perf_counter() - started
        self._metrics.step_duration(name, duration)
        self._log.debug("analysis.step", step=name, duration_ms=round(duration * 1000, 2))


class _LeaseControl:
    """Продление аренды и проверка отмены между шагами конвейера."""

    def __init__(
        self,
        analyses: AnalysisRepository,
        analysis_id: str,
        owner: str,
        clock: SyncClock,
        config: RunAnalysisConfig,
    ) -> None:
        self._analyses = analyses
        self._analysis_id = analysis_id
        self._owner = owner
        self._clock = clock
        self._config = config
        self._last_beat = clock.monotonic()

    def check(self) -> None:
        """Продлевает аренду не чаще раза в `heartbeat_seconds` и реагирует на отмену."""
        now = self._clock.monotonic()
        if now - self._last_beat < self._config.heartbeat_seconds:
            return
        self._last_beat = now
        state = self._analyses.heartbeat(self._analysis_id, self._owner, self._config.lease_seconds)
        if not state.alive:
            raise LeaseLost(f"аренда анализа {self._analysis_id} потеряна")
        if state.cancel_requested:
            raise _CancelRequested()


class _NoDocuments(Exception):
    """Нет документов для анализа."""


class _EmbeddingFailed(Exception):
    """Отказ эмбеддера."""


class _ModelFailed(Exception):
    """Отказ модели при скоринге."""


def _better_signal(
    current: EncyclopediaSignal | None, hit: EncyclopediaHit
) -> EncyclopediaSignal:
    """Лучший результат энциклопедии: существующая статья с большей посещаемостью."""
    candidate = EncyclopediaSignal(
        exists=hit.exists,
        pageviews_30d=max(hit.pageviews_30d, 0),
        created_at=hit.created_at,
    )
    if current is None:
        return candidate
    if candidate.exists and (not current.exists or candidate.pageviews_30d > current.pageviews_30d):
        return candidate
    return current
