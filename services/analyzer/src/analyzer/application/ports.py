"""Порты прикладного слоя analyzer (`typing.Protocol`).

Сервис синхронный (ADR-09: gRPC sync + ThreadPool, psycopg sync), поэтому все порты синхронные.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Protocol

import numpy as np

from analyzer.application.dto import (
    AnalysisDraft,
    CachedEmbedding,
    CollectionInfo,
    EncyclopediaHit,
    LeaseState,
    ModelBundle,
)
from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.entities import Analysis, Candidate, DocumentRef, ModelVersion
from analyzer.domain.values import AnalysisStats, Decision, OperationStatus


class AnalysisRepository(Protocol):
    """Хранилище анализов (таблица `analyses`)."""

    def create_if_absent(self, draft: AnalysisDraft) -> tuple[Analysis, bool]:
        """Создаёт анализ; при конфликте по `idempotency_key` возвращает существующий и True."""

    def get(self, analysis_id: str) -> Analysis | None:
        """Анализ по идентификатору."""

    def count_pending(self) -> int:
        """Число анализов в статусах PENDING/RUNNING."""

    def claim_next(self, owner: str, lease_seconds: int) -> Analysis | None:
        """Захватывает следующий анализ очереди (`FOR UPDATE SKIP LOCKED`) и ставит RUNNING."""

    def heartbeat(self, analysis_id: str, owner: str, lease_seconds: int) -> LeaseState:
        """Продлевает аренду; сообщает, жива ли аренда и запрошена ли отмена."""

    def request_cancel(self, analysis_id: str) -> OperationStatus | None:
        """Ставит признак отмены; для PENDING сразу переводит в CANCELLED."""

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
        """Терминальное завершение анализа владельцем аренды."""

    def release_expired_leases(self) -> int:
        """Возвращает анализы с истёкшей арендой в PENDING."""


class CandidateRepository(Protocol):
    """Хранилище кандидатов, их признаков и документов."""

    def save_results(
        self,
        analysis_id: str,
        owner: str,
        candidates: Sequence[Candidate],
        stats: AnalysisStats,
        finished_at: datetime,
    ) -> bool:
        """Одной транзакцией: удалить кандидатов прошлой попытки, записать новых с признаками и
        документами, сохранить статистику и перевести анализ в COMPLETED.

        False означает, что аренда потеряна (строка анализа не обновилась) и данные не записаны.
        """

    def list_candidates(
        self,
        analysis_id: str,
        *,
        include_excluded: bool,
        after: tuple[int, float, int] | None,
        limit: int,
    ) -> tuple[list[Candidate], int]:
        """Страница кандидатов и общее число подходящих строк."""


class EmbeddingCache(Protocol):
    """Кеш эмбеддингов документов (таблица `document_embeddings`)."""

    def get_many(self, document_ids: Sequence[str], embedding_model: str) -> dict[str, CachedEmbedding]:
        """Записи кеша по идентификаторам документов."""

    def put_many(self, entries: Sequence[CachedEmbedding], embedding_model: str, dims: int) -> None:
        """Сохраняет вычисленные эмбеддинги."""


class ModelVersionRepository(Protocol):
    """Реестр версий модели (таблица `model_versions`)."""

    def activate(self, version: ModelVersion) -> None:
        """Регистрирует версию и делает её единственной активной."""

    def get_active(self) -> ModelVersion | None:
        """Активная версия модели, если она зарегистрирована."""


class CollectorReader(Protocol):
    """Клиент сервиса collector."""

    def get_collection(self, collection_id: str) -> CollectionInfo:
        """Статус и объём коллекции."""

    def stream_documents(self, collection_id: str, chunk_size: int, limit: int) -> Iterator[DocumentRef]:
        """Документы коллекции потоком, не более `limit` штук."""

    def check_encyclopedia(self, titles: Sequence[str], language_code: str) -> list[EncyclopediaHit]:
        """Проверка статей Wikipedia."""

    def start_enrichment(self, idempotency_key: str, title: str) -> str:
        """Запускает ENRICHMENT-сбор по названию технологии и возвращает `collection_id`."""


class ModelStore(Protocol):
    """Каталог артефактов модели (volume `model-store`)."""

    def load_active(self, registry: FeatureRegistry) -> ModelBundle:
        """Загружает `active/manifest.json` с проверкой sha256 всех артефактов."""

    def load_from_path(self, path: str, registry: FeatureRegistry) -> ModelBundle:
        """Загружает конкретную версию модели (команда `register-model`)."""


class Embedder(Protocol):
    """Локальная модель эмбеддингов (e5)."""

    @property
    def model_name(self) -> str:
        """Идентификатор модели (должен совпадать с манифестом активной модели)."""

    @property
    def dims(self) -> int:
        """Размерность вектора."""

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов с префиксом e5 (`passage:` или `query:`), L2-нормализованные."""


class ClassifierModel(Protocol):
    """Калиброванная модель классификации на 24 признаках реестра."""

    @property
    def feature_names(self) -> tuple[str, ...]:
        """Имена признаков в порядке вектора модели."""

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность слабого сигнала для каждой строки."""

    def contributions(self, features: np.ndarray) -> np.ndarray:
        """Вклады признаков в логит (LR: коэффициент × стандартизованное значение; GBM: SHAP)."""

    def predict_stage(self, features: np.ndarray) -> list[int | None]:
        """Предсказанная стадия 1..4 или None, если вспомогательная модель отсутствует."""

    def predict_trend(self, features: np.ndarray) -> list[int | None]:
        """Предсказанный тренд 1..3 или None, если вспомогательная модель отсутствует."""


class MetricsSink(Protocol):
    """Приём метрик анализа."""

    def embeddings_computed(self, count: int) -> None:
        """Число вычисленных эмбеддингов."""

    def embedding_cache_hits(self, count: int) -> None:
        """Число попаданий в кеш эмбеддингов."""

    def step_duration(self, step: str, seconds: float) -> None:
        """Длительность шага конвейера."""

    def candidate_decided(self, decision: Decision) -> None:
        """Решение по кандидату."""

    def analysis_finished(self, status: OperationStatus) -> None:
        """Завершение анализа."""

    def model_activated(self, model_version_id: str) -> None:
        """Активация версии модели."""


class NullMetrics:
    """Метрики отключены (тесты и сценарии без экспортера)."""

    def embeddings_computed(self, count: int) -> None:
        """Ничего не делает."""

    def embedding_cache_hits(self, count: int) -> None:
        """Ничего не делает."""

    def step_duration(self, step: str, seconds: float) -> None:
        """Ничего не делает."""

    def candidate_decided(self, decision: Decision) -> None:
        """Ничего не делает."""

    def analysis_finished(self, status: OperationStatus) -> None:
        """Ничего не делает."""

    def model_activated(self, model_version_id: str) -> None:
        """Ничего не делает."""
