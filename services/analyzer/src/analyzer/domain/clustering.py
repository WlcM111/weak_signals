"""Near-dup, агломеративная кластеризация и отбор кандидатов (§12.5 ТЗ, шаги 3 и 5).

Кластеризация — average linkage по косинусному расстоянию с порогом (scipy: та же численная
библиотека, что уже стоит за scikit-learn; инфраструктурных зависимостей домен не получает).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist

from analyzer.domain.entities import DocumentRef
from analyzer.domain.values import TrustLevel

FAR_FUTURE_ORDER = 10**12


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """L2-нормализация строк матрицы (нулевые строки остаются нулевыми)."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return vectors / norms


def centroid(vectors: np.ndarray) -> np.ndarray:
    """Нормализованный центроид набора векторов."""
    if vectors.size == 0:
        return np.zeros(0, dtype=np.float32)
    mean = vectors.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    return mean if norm == 0.0 else mean / norm


def near_dup_filter(
    documents: Sequence[DocumentRef], vectors: np.ndarray, threshold: float
) -> tuple[list[int], list[int]]:
    """Удаляет near-дубликаты (косинус ≥ порога), оставляя более доверенный и более ранний документ.

    Возвращает индексы оставленных документов (в исходном порядке) и индексы отброшенных.
    """
    if len(documents) == 0:
        return [], []
    normalized = normalize_rows(np.asarray(vectors, dtype=np.float32))
    order = sorted(range(len(documents)), key=lambda index: _dedup_priority(documents[index]))
    kept: list[int] = []
    dropped: list[int] = []
    kept_matrix = np.zeros((0, normalized.shape[1]), dtype=np.float32)
    for index in order:
        vector = normalized[index]
        if kept_matrix.shape[0] and float(np.max(kept_matrix @ vector)) >= threshold:
            dropped.append(index)
            continue
        kept.append(index)
        kept_matrix = np.vstack([kept_matrix, vector])
    return sorted(kept), sorted(dropped)


def _dedup_priority(document: DocumentRef) -> tuple[int, int, int]:
    """Приоритет при дедупликации: выше доверенность, затем более ранняя дата, затем ранг."""
    published_order = (
        int(document.published_at.timestamp()) if document.published_at else FAR_FUTURE_ORDER
    )
    return (-document.trust_level.rank, published_order, document.relevance_rank)


def cluster_documents(vectors: np.ndarray, distance_threshold: float) -> list[list[int]]:
    """Агломеративная кластеризация; возвращает списки индексов документов по кластерам."""
    count = int(np.asarray(vectors).shape[0])
    if count == 0:
        return []
    if count == 1:
        return [[0]]
    normalized = normalize_rows(np.asarray(vectors, dtype=np.float64))
    distances = pdist(normalized, metric="cosine")
    distances = np.clip(distances, 0.0, 2.0)
    labels = fcluster(linkage(distances, method="average"), t=distance_threshold, criterion="distance")
    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(int(label), []).append(index)
    return [groups[label] for label in sorted(groups)]


def split_small_clusters(
    clusters: Sequence[Sequence[int]], documents: Sequence[DocumentRef], min_cluster_size: int
) -> tuple[list[list[int]], list[int]]:
    """Отделяет кластеры меньше минимального размера в «прочее».

    Исключение (§12.5): одиночный документ высокой доверенности остаётся кандидатом — решение по нему
    принимает правило SINGLE_SOURCE, а не молчаливое отбрасывание.
    """
    kept: list[list[int]] = []
    misc: list[int] = []
    for cluster in clusters:
        indexes = list(cluster)
        if len(indexes) >= min_cluster_size:
            kept.append(indexes)
            continue
        if len(indexes) == 1 and documents[indexes[0]].trust_level is TrustLevel.HIGH:
            kept.append(indexes)
            continue
        misc.extend(indexes)
    return kept, misc


# Вес документа при отборе кандидатов по доверенности источника (включается WS_TRUST_WEIGHTED_SELECTION).
TRUST_WEIGHTS = {TrustLevel.HIGH: 1.0, TrustLevel.MEDIUM: 0.5, TrustLevel.LOW: 0.25}


def trust_weights(documents: Sequence[DocumentRef]) -> list[float]:
    """Веса доверенности документов в порядке их индексов."""
    return [TRUST_WEIGHTS.get(document.trust_level, TRUST_WEIGHTS[TrustLevel.LOW]) for document in documents]


def select_top_clusters(
    clusters: Sequence[Sequence[int]],
    relevances: Sequence[float],
    max_candidates: int,
    weights: Sequence[float] | None = None,
) -> list[list[int]]:
    """Оставляет `max_candidates` кластеров по произведению размера на среднюю релевантность.

    С `weights` вклад каждого документа умножается на вес его доверенности: кластер из десятка
    однотипных репозиториев средней доверенности перестаёт вытеснять кластеры с публикациями
    высокой доверенности. Без `weights` формула прежняя.
    """
    scored = [
        (
            len(cluster) * (sum(relevances[index] for index in cluster) / max(len(cluster), 1))
            if weights is None
            else sum(relevances[index] * weights[index] for index in cluster),
            -min(cluster),
            list(cluster),
        )
        for cluster in clusters
    ]
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [cluster for _, _, cluster in scored[:max_candidates]]
    return sorted(selected, key=min)
