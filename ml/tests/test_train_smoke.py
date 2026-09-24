"""Быстрое обучение на подвыборке: детерминизм, честность протокола, экспорт артефакта."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from analyzer.adapters.outbound.config_loader import load_feature_registry
from analyzer.adapters.outbound.model_store import FileSystemModelStore
from analyzer.domain.rules import RuleThresholds

from ml.adapters.embedders import HashingEmbedder
from ml.adapters.model_store_fs import FileSystemModelStoreWriter
from ml.dataset import read_jsonl
from ml.export import build_manifest, build_rules_payload, calibrate_rule_thresholds, next_version_id
from ml.features import FeatureContext, empty_collection_defaults
from ml.train import train_model

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "ml" / "data" / "labels" / "dataset_ds-2026.09.16-v1.jsonl"
NOW = datetime(2026, 9, 16, tzinfo=UTC)
SEED = 20260915


def build_context() -> FeatureContext:
    """Контекст признаков из конфигурации сервиса."""
    return FeatureContext.load(
        ROOT / "schemas" / "feature_registry_v1.json",
        ROOT / "services" / "analyzer" / "config" / "lexicons",
        ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
        NOW,
    )


def run(rows, context, vectors):  # noqa: ANN001, ANN201 - внутренний помощник теста
    """Запуск обучения с фиксированными параметрами."""
    return train_model(
        rows=rows,
        context=context,
        vectors=vectors,
        enrichments=[None] * len(rows),
        seed=SEED,
        holdout_share=0.2,
        cv_repeats=1,
        cv_folds=3,
        min_precision=0.8,
        smoke=True,
    )


@unittest.skipUnless(DATASET.is_file(), "нужен собранный набор ml/data/labels/dataset_*.jsonl")
class TrainSmokeTest(unittest.TestCase):
    """Обучение на 40 строках проходит и даёт воспроизводимый результат."""

    @classmethod
    def setUpClass(cls) -> None:
        all_rows = read_jsonl(DATASET)
        positives = [row for row in all_rows if row.label == 1][:20]
        negatives = [row for row in all_rows if row.label == 0][:20]
        cls.rows = [*positives, *negatives]
        cls.context = build_context()
        cls.vectors = HashingEmbedder().encode([row.text for row in cls.rows], "passage: ")
        cls.result = run(cls.rows, cls.context, cls.vectors)

    def test_holdout_is_disjoint_from_train(self) -> None:
        self.assertEqual(set(self.result.train_index) & set(self.result.test_index), set())
        self.assertEqual(
            len(self.result.train_index) + len(self.result.test_index), len(self.rows)
        )

    def test_metrics_present(self) -> None:
        for name in ("accuracy", "precision", "recall", "f1", "roc_auc"):
            self.assertIn(name, self.result.test_metrics)
            self.assertGreaterEqual(self.result.test_metrics[name], 0.0)
            self.assertLessEqual(self.result.test_metrics[name], 1.0)

    def test_threshold_in_range(self) -> None:
        self.assertGreater(self.result.model.threshold, 0.0)
        self.assertLess(self.result.model.threshold, 1.0)

    def test_deterministic_between_runs(self) -> None:
        repeated = run(self.rows, self.context, self.vectors)
        self.assertEqual(repeated.test_metrics, self.result.test_metrics)
        self.assertEqual(repeated.model.threshold, self.result.model.threshold)

    def test_baselines_reported(self) -> None:
        self.assertIn("B0_rules_lexicon", self.result.baselines)
        self.assertIn("B1_tfidf_lr", self.result.baselines)
        self.assertIn("B2_embeddings_lr", self.result.baselines)

    def test_calibration_produces_probabilities(self) -> None:
        self.assertGreaterEqual(self.result.calibration["ece"], 0.0)
        self.assertLessEqual(self.result.calibration["ece"], 1.0)

    def test_artifact_is_readable_by_analyzer(self) -> None:
        registry = load_feature_registry(ROOT / "schemas" / "feature_registry_v1.json")
        positives = [
            self.result.static_values[index]
            for index in self.result.train_index
            if self.rows[index].label == 1
        ]
        thresholds, share, _ = calibrate_rule_thresholds(positives, 0.05)
        defaults = empty_collection_defaults(self.context, self.result.static_values)
        with tempfile.TemporaryDirectory() as tmp:
            version = next_version_id(Path(tmp), NOW)
            manifest = build_manifest(
                version_id=version,
                dataset_version="ds-2026.09.16-v1",
                embedding_model="hashing-char-ngram-256",
                threshold=self.result.model.threshold,
                test_metrics=self.result.test_metrics,
                cv_metrics=self.result.cv_metrics,
            )
            directory = FileSystemModelStoreWriter(Path(tmp)).export(
                version,
                {
                    "classifier": self.result.model.classifier,
                    "scaler": self.result.model.scaler,
                    "calibrator": self.result.model.calibrator,
                    "weak_centroid": self.result.model.weak_centroid,
                    "mature_centroid": self.result.model.mature_centroid,
                    "rules": build_rules_payload(thresholds, defaults),
                },
                manifest,
            )
            self.assertTrue((directory / "manifest.json").is_file())
            self.assertTrue((directory / "feature_defaults.json").is_file())
            bundle = FileSystemModelStore(Path(tmp)).load_active(registry)
            self.assertEqual(bundle.version.model_version_id, version)
            self.assertEqual(bundle.version.feature_schema_version, "v1")
            self.assertEqual(len(bundle.feature_defaults), len(defaults))
            matrix = np.asarray([registry.model_vector(dict.fromkeys(registry.names, 0.4))])
            score = float(bundle.classifier.predict_proba(matrix)[0])
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)
        self.assertLessEqual(share, 1.0)

    def test_manifest_matches_schema_fields(self) -> None:
        manifest = build_manifest(
            version_id="wsclf-2026.09.16-1",
            dataset_version="ds-2026.09.16-v1",
            embedding_model="intfloat/multilingual-e5-base",
            threshold=0.42,
            test_metrics=self.result.test_metrics,
            cv_metrics=self.result.cv_metrics,
        )
        schema = json.loads(
            (ROOT / "schemas" / "model_manifest.schema.json").read_text(encoding="utf-8")
        )
        for field in schema["required"]:
            if field == "artifact_files":
                continue
            self.assertIn(field, manifest)


class RuleCalibrationTest(unittest.TestCase):
    """Калибровка порогов правил ослабляет их до допустимой доли исключённых позитивов."""

    def test_relaxes_until_share_is_acceptable(self) -> None:
        strict = RuleThresholds(maturity_min=0.0, emergence_max=1.0)
        values = [
            {"emb_sim_query": 1.0, "lex_maturity_score": 0.9, "lex_emergence_score": 0.1,
             "wiki_exists": 0.0, "share_marketing": 0.0, "share_scientific": 0.5,
             "lex_hype_score": 0.0, "trusted_share": 0.5, "stage_lex_ordinal": 2.0,
             "bigtech_mentions_count": 0.0, "share_code_vacancy": 0.0,
             "wiki_pageviews_30d_log": 0.0, "wiki_age_years": 0.0}
            for _ in range(10)
        ]
        _, share, _ = calibrate_rule_thresholds(values, 0.05, strict)
        self.assertLessEqual(share, 0.05)

    def test_default_thresholds_on_neutral_rows(self) -> None:
        values = [
            {"emb_sim_query": 1.0, "lex_maturity_score": 0.1, "lex_emergence_score": 0.5,
             "wiki_exists": 0.0, "share_marketing": 0.1, "share_scientific": 0.4,
             "lex_hype_score": 0.1, "trusted_share": 0.6, "stage_lex_ordinal": 2.0,
             "bigtech_mentions_count": 0.0, "share_code_vacancy": 0.0,
             "wiki_pageviews_30d_log": 0.0, "wiki_age_years": 0.0}
        ]
        thresholds, share, reasons = calibrate_rule_thresholds(values, 0.05)
        self.assertEqual(share, 0.0)
        self.assertEqual(reasons, {})
        self.assertEqual(thresholds, RuleThresholds())


if __name__ == "__main__":
    unittest.main()
