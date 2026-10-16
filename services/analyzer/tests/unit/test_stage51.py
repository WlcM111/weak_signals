"""Этап 5.1 (ревью e2e 5.0.4): порог кластеризации по коллекции, отбор моделью, относительное правило близости,
известные зрелые области."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import yaml

from analyzer.application.use_cases.run_analysis import RunAnalysisConfig, relevance_floor, select_by_score
from analyzer.domain.clustering import adaptive_threshold, cluster_documents
from analyzer.domain.rules import mature_field
from analyzer.domain.values import OperationStatus

from .test_run_analysis import RunAnalysisHarness, emerging_documents, marketing_documents, mature_documents

ROOT = Path(__file__).resolve().parents[4]


def _compressed(n_groups: int = 6, per_group: int = 8, dim: int = 64, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    common = rng.normal(size=dim) * 6.0
    groups = rng.normal(size=(n_groups, dim))
    return np.asarray([common + groups[g] + rng.normal(scale=0.3, size=dim)
                       for g in range(n_groups) for _ in range(per_group)])


class AdaptiveThresholdTest(unittest.TestCase):
    def test_threshold_follows_nearest_neighbours_and_bounds(self) -> None:
        vectors = _compressed()
        threshold = adaptive_threshold(vectors, True, 0.6, 0.30, 0.85)
        self.assertTrue(0.30 <= threshold <= 0.85)
        clusters = cluster_documents(vectors, threshold, centered=True)
        singletons = sum(1 for cluster in clusters if len(cluster) == 1)
        self.assertLessEqual(singletons / len(vectors), 0.5)  # ревью: было 76–80 %
        self.assertEqual(adaptive_threshold(vectors[:2], True, 0.6, 0.30, 0.85), 0.85)  # вырожденная коллекция


class SelectionTest(unittest.TestCase):
    def test_best_by_model_with_market_reserve(self) -> None:
        scores = [0.1, 0.9, 0.5, 0.8, 0.2, 0.7]
        market = [True, False, False, False, True, False]
        self.assertEqual(select_by_score(scores, 3, market, 0), [1, 3, 5])
        self.assertEqual(select_by_score(scores, 3, market, 1), [1, 3, 4])  # лучшая заметка СМИ — в резерве

    def test_relative_floor_marks_only_outliers(self) -> None:
        values = [0.85, 0.86, 0.84, 0.87, 0.85, 0.86, 0.70]
        floor = relevance_floor(values, 3.0)
        self.assertIsNotNone(floor)
        self.assertEqual([value for value in values if value < floor], [0.70])
        self.assertIsNone(relevance_floor(values, 0.0))


class MatureFieldTest(unittest.TestCase):
    def test_bare_mature_fields_and_specific_applications(self) -> None:
        for title in ("AI-Powered Predictive Maintenance", "Industrial Defect Detection", "Robo Advising",
                      "Secure Multi-Party Computation", "IoT Digital Twins", "Soft Robotics"):
            self.assertIsNotNone(mature_field(title), title)
        for title in ("federated learning for distributed factories", "ClawLess A Security", "Cognitive robotic tool",
                      "Infer-EDGE Dynamic DNN", "FissionReady Joint Workload"):
            self.assertIsNone(mature_field(title), title)

    def test_no_expert_signal_is_called_mature(self) -> None:
        dataset = ROOT / "ml/data/labels/dataset_ds-2026.09.19-v2.jsonl"
        synonyms_path = ROOT / "ml/data/benchmark/expert_synonyms.yaml"
        if not dataset.exists() or not synonyms_path.exists():
            self.skipTest("нет эталона")
        synonyms = yaml.safe_load(synonyms_path.read_text(encoding="utf-8"))["synonyms"]
        rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
        flagged = [(row["row_id"], text) for row in rows if int(row.get("label") or 0) == 1
                   for text in [row["title"], *(synonyms.get(row["row_id"]) or [])] if mature_field(text)]
        self.assertEqual(flagged, [])


if __name__ == "__main__":
    unittest.main()



class PreselectRunTest(RunAnalysisHarness):
    """Полный прогон: модель выбирает кандидатов среди всех кластеров, когда их больше лимита."""

    def test_model_chooses_candidates_among_all_clusters(self) -> None:
        documents = emerging_documents() + mature_documents() + marketing_documents()
        config = RunAnalysisConfig(min_doc_query_sim=0.0, min_cluster_size=1, cluster_distance_threshold=0.0001,
                                   preselect_by_model=True, candidate_relevance_outlier_mad=3.0)
        use_case, analysis, _analyses, candidates, _ = self.build(documents, config=config)
        analysis.params = replace(analysis.params, max_candidates=5)
        status = use_case.execute(analysis, "worker-1")
        self.assertIs(status, OperationStatus.COMPLETED)
        self.assertEqual(len(candidates.saved[analysis.analysis_id]), 5)  # 10 кластеров → 5 лучших по модели
