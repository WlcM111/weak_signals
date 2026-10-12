"""Этап 5.0 (аудит раунда 3): кластеризация и отбор в центрированном пространстве, выбросы релевантности."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from analyzer.application.use_cases.run_analysis import selection_relevance
from analyzer.domain.clustering import center_rows, cluster_documents

NPZ = Path(__file__).resolve().parents[4] / "ml/reports/experiments/cache/emb-intfloat_multilingual_e5_base.npz"


def _compressed(n_groups: int = 6, per_group: int = 8, dim: int = 64, seed: int = 0) -> np.ndarray:
    """Векторы со сжатой шкалой, как у e5: общая компонента + слабые различия групп."""
    rng = np.random.default_rng(seed)
    common = rng.normal(size=dim) * 6.0
    groups = rng.normal(size=(n_groups, dim))
    rows = [common + groups[g] + rng.normal(scale=0.3, size=dim) for g in range(n_groups) for _ in range(per_group)]
    return np.asarray(rows)


class CenteredClusteringTest(unittest.TestCase):
    def test_raw_collapses_but_centered_recovers_groups(self) -> None:
        vectors = _compressed()
        self.assertEqual(len(cluster_documents(vectors, 0.35)), 1)  # прежнее поведение: один кластер
        clusters = cluster_documents(vectors, 0.60, centered=True)
        self.assertEqual(sorted(len(c) for c in clusters), [8] * 6)
        self.assertAlmostEqual(float(np.abs(center_rows(vectors).mean(axis=0)).max()), 0.0, places=9)

    @unittest.skipUnless(NPZ.exists(), "нет кеша эмбеддингов")
    def test_real_e5_embeddings_are_not_one_blob(self) -> None:
        data = np.load(NPZ)["vectors"][:400].astype("float64")
        self.assertEqual(len(cluster_documents(data, 0.35)), 1)  # аудит N01
        sizes = sorted((len(c) for c in cluster_documents(data, 0.60, centered=True)), reverse=True)
        self.assertGreater(len(sizes), 100)
        self.assertLess(sizes[0], 40)


class SelectionRelevanceTest(unittest.TestCase):
    def test_only_clear_outliers_fall_below_the_floor(self) -> None:
        vectors = _compressed(n_groups=3, per_group=10)
        query = vectors[:10].mean(axis=0)
        values, floor = selection_relevance(vectors, query, None, "mean", True, 3.0)
        self.assertEqual(len(values), 30)
        self.assertLess(int((values < floor).sum()), 3)  # отсекаются единицы, а не треть
        _, no_floor = selection_relevance(vectors, query, None, "mean", True, 0.0)
        self.assertEqual(no_floor, float("-inf"))


if __name__ == "__main__":
    unittest.main()
