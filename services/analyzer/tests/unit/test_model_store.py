"""Загрузка артефактов модели: манифест, sha256, центроиды, обёртка scikit-learn (§17 HANDOFF)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from analyzer.adapters.outbound.model_store import FileSystemModelStore, ModelStoreError
from analyzer.adapters.outbound.sklearn_model import SklearnClassifier
from analyzer.adapters.outbound.config_loader import load_feature_registry
from ..model_fixture import MODEL_VERSION_ID, THRESHOLD, build_model_store

REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = load_feature_registry(REPO_ROOT / "schemas" / "feature_registry_v1.json")


class ModelStoreTest(unittest.TestCase):
    """Успешная загрузка и отказы при нарушениях."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_active_model(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        bundle = FileSystemModelStore(self.root).load_active(REGISTRY)
        self.assertEqual(bundle.version.model_version_id, MODEL_VERSION_ID)
        self.assertEqual(bundle.threshold, THRESHOLD)
        self.assertEqual(bundle.version.artifact_path, "active")
        self.assertEqual(len(bundle.version.artifact_sha256), 64)
        self.assertEqual(bundle.version.feature_schema_version, "v1")
        self.assertIsNotNone(bundle.weak_centroid)
        self.assertIsNotNone(bundle.mature_centroid)

    def test_reads_thresholds_and_defaults_from_rules_json(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        bundle = FileSystemModelStore(self.root).load_active(REGISTRY)
        self.assertEqual(bundle.thresholds.min_query_similarity, 0.30)
        self.assertEqual(bundle.feature_defaults["recency_median_days"], 400.0)

    def test_rejects_corrupted_artifact(self) -> None:
        build_model_store(self.root, REGISTRY.model_names, corrupt=True)
        with self.assertRaises(ModelStoreError) as error:
            FileSystemModelStore(self.root).load_active(REGISTRY)
        self.assertIn("контрольная сумма", str(error.exception))

    def test_detects_modified_artifact(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        (self.root / "active" / "classifier.joblib").write_bytes("подмена".encode("utf-8"))
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)

    def test_missing_manifest(self) -> None:
        (self.root / "active").mkdir(parents=True)
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)

    def test_missing_directory(self) -> None:
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)

    def test_rejects_path_outside_store(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_from_path("/etc", REGISTRY)

    def test_rejects_bad_version_id(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        manifest_path = self.root / "active" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["model_version_id"] = "model-1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)

    def test_rejects_threshold_outside_range(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        manifest_path = self.root / "active" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["threshold"] = 1.0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)

    def test_requires_checksums_by_default(self) -> None:
        build_model_store(self.root, REGISTRY.model_names)
        manifest_path = self.root / "active" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_files"]["sha256"].pop("classifier")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(self.root).load_active(REGISTRY)
        # с выключенной проверкой загрузка проходит (режим отладки)
        bundle = FileSystemModelStore(self.root, require_sha256=False).load_active(REGISTRY)
        self.assertEqual(bundle.version.model_version_id, MODEL_VERSION_ID)


class SklearnClassifierTest(unittest.TestCase):
    """Скоринг и вклады на настоящем обученном артефакте."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        build_model_store(root, REGISTRY.model_names)
        cls.bundle = FileSystemModelStore(root).load_active(REGISTRY)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def matrix(self, value: float) -> np.ndarray:
        """Матрица из одной строки с одинаковыми значениями признаков."""
        return np.full((1, len(REGISTRY.model_names)), value, dtype=np.float64)

    def test_feature_names_match_registry(self) -> None:
        self.assertEqual(self.bundle.classifier.feature_names, REGISTRY.model_names)

    def test_probability_in_unit_range(self) -> None:
        score = float(self.bundle.classifier.predict_proba(self.matrix(0.0))[0])
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_positive_direction_scores_higher(self) -> None:
        low = float(self.bundle.classifier.predict_proba(self.matrix(-1.0))[0])
        high = float(self.bundle.classifier.predict_proba(self.matrix(1.0))[0])
        self.assertGreater(high, low)

    def test_contributions_shape(self) -> None:
        contributions = self.bundle.classifier.contributions(self.matrix(0.5))
        self.assertEqual(contributions.shape, (1, len(REGISTRY.model_names)))

    def test_batch_scoring(self) -> None:
        batch = np.vstack([self.matrix(-1.0), self.matrix(1.0)])
        scores = self.bundle.classifier.predict_proba(batch)
        self.assertEqual(scores.shape, (2,))
        self.assertLess(scores[0], scores[1])

    def test_auxiliary_models_absent(self) -> None:
        self.assertEqual(self.bundle.classifier.predict_stage(self.matrix(0.0)), [None])
        self.assertEqual(self.bundle.classifier.predict_trend(self.matrix(0.0)), [None])


class SklearnFallbackTest(unittest.TestCase):
    """Поведение обёртки без scaler и без калибратора."""

    class _Linear:
        """Минимальная линейная модель с коэффициентами."""

        coef_ = np.asarray([[1.0, -2.0]])

        def decision_function(self, features: np.ndarray) -> np.ndarray:
            """Логит как скалярное произведение."""
            return features @ self.coef_[0]

    def test_sigmoid_applied_without_calibrator(self) -> None:
        model = SklearnClassifier(None, self._Linear(), None, ("a", "b"), "logreg_elasticnet")
        scores = model.predict_proba(np.asarray([[0.0, 0.0], [10.0, 0.0]]))
        self.assertAlmostEqual(float(scores[0]), 0.5, places=6)
        self.assertGreater(float(scores[1]), 0.99)

    def test_contributions_are_coefficient_times_value(self) -> None:
        model = SklearnClassifier(None, self._Linear(), None, ("a", "b"), "logreg_elasticnet")
        contributions = model.contributions(np.asarray([[2.0, 3.0]]))
        self.assertAlmostEqual(float(contributions[0][0]), 2.0)
        self.assertAlmostEqual(float(contributions[0][1]), -6.0)


if __name__ == "__main__":
    unittest.main()
