"""Near-dup, кластеризация на синтетических группах и отбор кандидатов (§12.5, шаги 3 и 5)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import numpy as np

from analyzer.domain.clustering import (
    centroid,
    cluster_documents,
    near_dup_filter,
    normalize_rows,
    select_top_clusters,
    split_small_clusters,
)
from analyzer.domain.values import TrustLevel
from ..fakes import make_document


def group_vectors(axis: int, count: int, dims: int = 8, noise: float = 0.02) -> np.ndarray:
    """Векторы вокруг одной оси — синтетическая тематическая группа."""
    generator = np.random.default_rng(axis)
    vectors = np.zeros((count, dims), dtype=np.float32)
    vectors[:, axis] = 1.0
    vectors += generator.normal(scale=noise, size=(count, dims)).astype(np.float32)
    return normalize_rows(vectors)


class ClusterDocumentsTest(unittest.TestCase):
    """Три хорошо разделённые группы должны дать ровно три кластера."""

    def test_three_groups(self) -> None:
        vectors = np.vstack([group_vectors(0, 4), group_vectors(3, 3), group_vectors(6, 5)])
        clusters = cluster_documents(vectors, distance_threshold=0.35)
        self.assertEqual(len(clusters), 3)
        self.assertEqual(sorted(len(cluster) for cluster in clusters), [3, 4, 5])

    def test_each_document_belongs_to_exactly_one_cluster(self) -> None:
        vectors = np.vstack([group_vectors(0, 3), group_vectors(4, 3)])
        clusters = cluster_documents(vectors, 0.35)
        flat = sorted(index for cluster in clusters for index in cluster)
        self.assertEqual(flat, list(range(6)))

    def test_empty_and_single(self) -> None:
        self.assertEqual(cluster_documents(np.zeros((0, 8), dtype=np.float32), 0.35), [])
        self.assertEqual(cluster_documents(group_vectors(1, 1), 0.35), [[0]])

    def test_large_threshold_merges_everything(self) -> None:
        vectors = np.vstack([group_vectors(0, 3), group_vectors(5, 3)])
        self.assertEqual(len(cluster_documents(vectors, 1.5)), 1)


class NearDupTest(unittest.TestCase):
    """Дубликаты удаляются, остаётся более доверенный и более ранний документ."""

    def test_keeps_more_trusted(self) -> None:
        vectors = np.vstack([group_vectors(0, 1), group_vectors(0, 1)])
        documents = [
            make_document(1, "копия", trust_level=TrustLevel.LOW),
            make_document(2, "оригинал", trust_level=TrustLevel.HIGH),
        ]
        kept, dropped = near_dup_filter(documents, vectors, threshold=0.9)
        self.assertEqual(kept, [1])
        self.assertEqual(dropped, [0])

    def test_keeps_earlier_when_trust_equal(self) -> None:
        vectors = np.vstack([group_vectors(0, 1), group_vectors(0, 1)])
        documents = [
            make_document(1, "поздний", published_at=datetime(2026, 5, 1, tzinfo=UTC)),
            make_document(2, "ранний", published_at=datetime(2025, 1, 1, tzinfo=UTC)),
        ]
        kept, _ = near_dup_filter(documents, vectors, threshold=0.9)
        self.assertEqual(kept, [1])

    def test_distinct_documents_survive(self) -> None:
        vectors = np.vstack([group_vectors(0, 1), group_vectors(5, 1)])
        documents = [make_document(1, "A"), make_document(2, "B")]
        kept, dropped = near_dup_filter(documents, vectors, threshold=0.92)
        self.assertEqual(kept, [0, 1])
        self.assertEqual(dropped, [])

    def test_empty_input(self) -> None:
        self.assertEqual(near_dup_filter([], np.zeros((0, 8), dtype=np.float32), 0.9), ([], []))


class SplitSmallClustersTest(unittest.TestCase):
    """Мелкие кластеры уходят в «прочее», кроме одиночных HIGH-документов."""

    def test_small_cluster_goes_to_misc(self) -> None:
        documents = [make_document(index, "x", trust_level=TrustLevel.MEDIUM) for index in range(3)]
        kept, misc = split_small_clusters([[0, 1], [2]], documents, min_cluster_size=2)
        self.assertEqual(kept, [[0, 1]])
        self.assertEqual(misc, [2])

    def test_single_high_trust_document_is_kept(self) -> None:
        documents = [
            make_document(0, "a", trust_level=TrustLevel.MEDIUM),
            make_document(1, "b", trust_level=TrustLevel.HIGH),
        ]
        kept, misc = split_small_clusters([[0], [1]], documents, min_cluster_size=2)
        self.assertEqual(kept, [[1]])
        self.assertEqual(misc, [0])


class SelectTopClustersTest(unittest.TestCase):
    """Отбор по размеру × средней релевантности."""

    def test_limits_count_and_keeps_best(self) -> None:
        clusters = [[0, 1, 2], [3], [4, 5]]
        relevances = [0.9, 0.9, 0.9, 0.99, 0.5, 0.5]
        selected = select_top_clusters(clusters, relevances, max_candidates=2)
        self.assertEqual(len(selected), 2)
        self.assertIn([0, 1, 2], selected)
        self.assertIn([4, 5], selected)

    def test_returns_all_when_limit_is_large(self) -> None:
        clusters = [[0], [1]]
        self.assertEqual(len(select_top_clusters(clusters, [0.5, 0.5], 10)), 2)


class CentroidTest(unittest.TestCase):
    """Центроид нормализован и близок к своей группе."""

    def test_unit_norm(self) -> None:
        vectors = group_vectors(2, 5)
        result = centroid(vectors)
        self.assertAlmostEqual(float(np.linalg.norm(result)), 1.0, places=5)
        self.assertGreater(float(vectors[0] @ result), 0.9)

    def test_empty(self) -> None:
        self.assertEqual(centroid(np.zeros((0, 4), dtype=np.float32)).size, 0)


if __name__ == "__main__":
    unittest.main()
