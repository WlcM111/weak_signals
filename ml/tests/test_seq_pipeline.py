"""Датасет B, признаки v2, Stage A → Stage B, защита знаний A, экспорт и загрузка артефакта analyzer."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.adapters.outbound.model_store import FileSystemModelStore, ModelStoreError
from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.use_cases.activate_model import ActivateModelFromStore as ActivateModel
from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig, _ClusterView
from analyzer.application.use_cases.score_text import ScoreText
from analyzer.domain.entities import DocumentRef
from analyzer.domain.features_v2 import FEATURE_NAMES_V2, TOPIC_DEPENDENT
from analyzer.domain.values import SourceType, TrustLevel

from ml.adapters.embedders import HashingEmbedder
from ml.config import REPO_ROOT, MlSettings
from ml.dataset_b.build import HOLDOUT_TOPICS, build
from ml.seq import linear
from ml.seq import metrics as M
from ml.seq.data import Obs, load_a, load_b
from ml.seq.experiments import load_glossary
from ml.seq.export import export
from ml.seq.featurize import Featurizer
from ml.seq.stages import B_MASK, TOPIC_INDEX, load_checkpoint, train_stage_a, train_stage_b

SETTINGS = MlSettings()
B_DIR = SETTINGS.data_dir / "dataset_b"
A_PATH = SETTINGS.labels_dir / "dataset_ds-2026.09.19-v2.jsonl"


def featurizer() -> Featurizer:
    return Featurizer(HashingEmbedder(), load_lexicons(SETTINGS.lexicon_dir, SETTINGS.stage_rules_path),
                      load_glossary(SETTINGS.lexicon_dir.parent / "glossary_ru_en.yaml"))


class DatasetBTest(unittest.TestCase):
    @unittest.skipUnless(list(REPO_ROOT.glob("analytics-*.txt")), "журналы прогонов не лежат в корне (образ trainer)")
    def test_build_is_deterministic_and_matches_committed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("labels_silver_v1.jsonl", "web_observations_v1.jsonl"):
                shutil.copy(B_DIR / name, Path(tmp) / name)
            report = build(REPO_ROOT, Path(tmp))
            self.assertEqual(report["status"], "ok", report["problems"])
            for name in ("dataset_b_v1.jsonl", "dataset_b_v1_uncertain.jsonl", "splits_v1.json"):
                self.assertEqual((Path(tmp) / name).read_text(encoding="utf-8"),
                                 (B_DIR / name).read_text(encoding="utf-8"), name)

    def test_splits_groups_dates_and_label_states(self) -> None:
        rows = [json.loads(x) for x in (B_DIR / "dataset_b_v1.jsonl").read_text(encoding="utf-8").splitlines()]
        uncertain = [json.loads(x) for x in (B_DIR / "dataset_b_v1_uncertain.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(r["label"] in (0, 1) for r in rows))
        self.assertTrue(all(r["label"] is None and r["label_kind"] == "UNCERTAIN" for r in uncertain))
        splits_by_group: dict[str, set[str]] = {}
        for row in rows + uncertain:
            splits_by_group.setdefault(row["group_id"], set()).add(row["split"])
            self.assertEqual(row["split"] == "holdout", row["topic"] in HOLDOUT_TOPICS)
            for ref in row["evidence_refs"]:
                if ref["published_at"]:
                    self.assertLessEqual(ref["published_at"][:10], row["evidence_as_of"])
            self.assertEqual(row["annotation_method"], "llm_assisted_silver")
        self.assertTrue(all(len(s) == 1 for s in splits_by_group.values()))
        kinds = {r["label_kind"] for r in rows if r["label"] == 0}
        self.assertTrue({"OFF_TOPIC", "GENERIC_CONCEPT", "OVERVIEW", "MATURE", "NOISE", "HYPE"} <= kinds)

    def test_label_rationale_and_runtime_scores_do_not_reach_features(self) -> None:
        lines = (B_DIR / "dataset_b_v1.jsonl").read_text(encoding="utf-8").splitlines()[:6]
        changed = []
        for line in lines:
            row = json.loads(line)
            row["label_rationale"] = "ПОЧЕМУ ЭТО СЛАБЫЙ СИГНАЛ " * 20
            row["label_kind"] = "WEAK_SIGNAL_RELEVANT"
            row["observed_runtime"]["score"] = 0.999
            changed.append(json.dumps(row, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.jsonl", Path(tmp) / "b.jsonl"
            first.write_text("\n".join(lines), encoding="utf-8")
            second.write_text("\n".join(changed), encoding="utf-8")
            feat = featurizer()
            a_rows = load_a(A_PATH, 1)[:20]
            projection = feat.fit_projection(a_rows)
            np.testing.assert_array_equal(feat.matrix(load_b(first), projection), feat.matrix(load_b(second), projection))


class SequentialTrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.feat = featurizer()
        a_rows = load_a(A_PATH, 20260915)
        cls.a_train = [o for o in a_rows if o.split == "train"]
        cls.a_dev = [o for o in a_rows if o.split == "dev"]
        b_rows = load_b(B_DIR / "dataset_b_v1.jsonl")
        cls.b_dev = [o for o in b_rows if o.split.startswith("dev")]
        cls.projection = cls.feat.fit_projection(cls.a_train)
        cls.Xa = cls.feat.matrix(cls.a_train, cls.projection)
        cls.ya = np.asarray([o.label for o in cls.a_train], dtype=float)
        cls.Xb = cls.feat.matrix(cls.b_dev, cls.projection)
        cls.yb = np.asarray([o.label for o in cls.b_dev], dtype=float)
        cls.stage_a = train_stage_a(cls.Xa, cls.ya, [o.group_id for o in cls.a_train], (1.0, 3.0), 7, cls.projection,
                                    cls.feat.embedding_model, cls.feat.glossary, {"a_train_ids": [o.sample_id for o in cls.a_train]})
        cls.tmp = tempfile.mkdtemp()
        cls.sha_a = cls.stage_a.save(Path(cls.tmp) / "stage_a.json")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_rows_have_no_topic_dependent_values(self) -> None:
        self.assertTrue(np.all(self.Xa[:, B_MASK] == 0.0))
        self.assertTrue(np.all(self.Xa[:, TOPIC_INDEX] == 0.0))
        self.assertTrue(np.all(self.Xb[:, TOPIC_INDEX] == 1.0))

    def test_stage_a_checkpoint_roundtrip(self) -> None:
        loaded, sha = load_checkpoint(Path(self.tmp) / "stage_a.json")
        self.assertEqual(sha, self.sha_a)
        self.assertEqual(loaded.stage, "A")
        self.assertIsNotNone(loaded.hessian_a)
        np.testing.assert_allclose(loaded.theta, self.stage_a.theta)

    def test_stage_b_continues_from_parent_with_lineage_and_train_only_replay(self) -> None:
        a_ids = [o.sample_id for o in self.a_train]
        ck = train_stage_b("seq_laplace_replay", {"alpha": 1.0, "rho": 0.3}, self.stage_a, self.sha_a,
                           self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        self.assertEqual(ck.parent_id, self.stage_a.checkpoint_id)
        self.assertEqual(ck.parent_sha256, self.sha_a)
        self.assertTrue(ck.params["uses_parent_state"])
        self.assertEqual(ck.params["started_from"], "parent")
        self.assertLess(ck.params["parent_hessian_recomputed_max_abs_diff"], 1e-8)
        self.assertTrue(set(ck.params["replay_ids"]) <= set(a_ids))
        self.assertFalse(set(ck.params["replay_ids"]) & {o.sample_id for o in self.a_dev})

    def test_converged_warm_start_equals_b_only(self) -> None:
        # warm start без штрафа — только инициализация: выпуклая задача сходится к решению «только B».
        a_ids = [o.sample_id for o in self.a_train]
        warm = train_stage_b("seq_finetune", {"l2": 3.0}, self.stage_a, self.sha_a, self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        cold = train_stage_b("b_only", {"l2": 3.0}, self.stage_a, self.sha_a, self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        np.testing.assert_allclose(warm.theta, cold.theta, atol=1e-3)

    def test_laplace_retains_stage_a_better_than_finetune(self) -> None:
        Xd = self.feat.matrix(self.a_dev, self.projection)
        yd = np.asarray([o.label for o in self.a_dev], dtype=float)
        a_ids = [o.sample_id for o in self.a_train]
        tuned = train_stage_b("seq_finetune", {"l2": 3.0}, self.stage_a, self.sha_a, self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        kept = train_stage_b("seq_laplace", {"alpha": 3.0}, self.stage_a, self.sha_a, self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        self.assertGreater(M.roc_auc(yd, kept.logits(Xd)), M.roc_auc(yd, tuned.logits(Xd)))

    def test_export_loads_in_analyzer_with_identical_probabilities_and_rejects_tampering(self) -> None:
        a_ids = [o.sample_id for o in self.a_train]
        ck = train_stage_b("seq_laplace", {"alpha": 1.0}, self.stage_a, self.sha_a, self.Xb, self.yb, self.Xa, self.ya, a_ids, {})
        ck.calibration, ck.threshold = {"slope": 0.8, "offset": -0.3}, 0.3
        path = Path(self.tmp) / "stage_b.json"
        ck.save(path)
        store_dir = Path(self.tmp) / "store"
        export(path, store_dir, "wsclf-2026.09.24-7", 0.8, activate=True)
        registry = load_feature_registry(SETTINGS.feature_registry_path)
        bundle = FileSystemModelStore(store_dir).load_active(registry)
        self.assertEqual(bundle.feature_schema, "v2")
        both = np.vstack([self.Xa[:10], self.Xb[:10]])
        np.testing.assert_allclose(bundle.classifier.predict_proba(both), ck.probability(both), atol=1e-9)
        versions = type("V", (), {"activate": lambda self, version: None})()
        ActivateModel(FileSystemModelStore(store_dir), registry, versions, ActiveModelHolder(),
                      embedder=HashingEmbedder()).execute()
        model_file = store_dir / "wsclf-2026.09.24-7" / "model_v2.json"
        model_file.write_text(model_file.read_text(encoding="utf-8").replace('"intercept": ', '"intercept": 1.0 + '),
                              encoding="utf-8")
        with self.assertRaises(ModelStoreError):
            FileSystemModelStore(store_dir).load_from_path("wsclf-2026.09.24-7", registry)
        with self.assertRaises(FileExistsError):
            export(path, store_dir, "wsclf-2026.09.24-7", 0.8, activate=False)


class RuntimeParityTest(unittest.TestCase):
    """Признаки, которые analyzer считает в RunAnalysis и ScoreText, совпадают с признаками обучения."""

    def setUp(self) -> None:
        self.feat = featurizer()
        self.a_rows = load_a(A_PATH, 20260915)[:30]
        self.projection = self.feat.fit_projection(self.a_rows)
        self.embedder = HashingEmbedder()
        self.lexicons = load_lexicons(SETTINGS.lexicon_dir, SETTINGS.stage_rules_path)
        spec = type("S", (), {"projection": self.projection, "glossary": self.feat.glossary,
                              "query_prefix": "query: ", "passage_prefix": "passage: "})()
        self.bundle = type("B", (), {"v2": spec, "feature_schema": "v2"})()

    def test_run_analysis_v2_features_equal_training_features(self) -> None:
        row = load_b(B_DIR / "dataset_b_v1.jsonl")[0]
        documents = [DocumentRef(document_id=f"d{i}", title=e.title, text="", language_code="en",
                                 source_type=SourceType(e.source_type), trust_level=TrustLevel(e.trust_level),
                                 origin_domain="x", published_at=datetime(e.published_year, 6, 1, tzinfo=UTC)
                                 if e.published_year else None) for i, e in enumerate(row.evidence)]
        view = _ClusterView(cluster_index=0, documents=documents, similarities=[1.0 - 0.01 * i for i in range(len(documents))],
                            centroid=np.zeros(3), title_auto=row.title, keyphrases=(row.title,),
                            query_relevance=row.observed_query_sim or 0.0, values={})
        runner = type("R", (), {})()
        runner._config = RunAnalysisConfig(evidence_max=8)
        runner._embedder = self.embedder
        runner._lexicons = self.lexicons
        runner._encode_batched = lambda texts, prefix: RunAnalysis._encode_batched(runner, texts, prefix)
        RunAnalysis._add_v2_features(runner, [view], f"{row.topic} | extra phrase", self.bundle, row.as_of_year)
        runtime = np.asarray([view.values[name] for name in FEATURE_NAMES_V2])
        training = self.feat.matrix([row], self.projection)[0]
        np.testing.assert_allclose(runtime, training, atol=1e-9)

    def test_score_text_v2_features_equal_a_row_features(self) -> None:
        row: Obs = self.a_rows[0]
        runner = type("R", (), {})()
        runner._embedder = self.embedder
        runner._lexicons = self.lexicons
        values = ScoreText._v2_values(runner, self.bundle, row.title, row.as_of_year)
        runtime = np.asarray([values[name] for name in FEATURE_NAMES_V2])
        np.testing.assert_allclose(runtime, self.feat.matrix([row], self.projection)[0], atol=1e-9)
        self.assertTrue(all(values[name] == 0.0 for name in TOPIC_DEPENDENT))


class MetricsTest(unittest.TestCase):
    def test_metrics_denominators_and_single_class(self) -> None:
        y = np.array([1, 0, 0, 1, 0, 0])
        s = np.array([0.9, 0.8, 0.1, 0.7, 0.2, 0.3])
        topics = ["t1", "t1", "t1", "t2", "t2", "t2"]
        p3, n3 = M.precision_at_k(y, s, topics, 3)
        self.assertEqual((round(p3, 4), n3), (round(1 / 3, 4), 2))
        self.assertIsNone(M.roc_auc(np.zeros(4), np.arange(4)))
        cal = M.platt(np.array([-2.0, -1.0, 1.0, 2.0]), np.array([0, 0, 1, 1]))
        self.assertGreater(cal["slope"], 0)
        theta, info = linear.fit([linear.Block(np.array([[0.0], [1.0]]), np.array([0.0, 1.0]), np.ones(2))], 1.0)
        self.assertTrue(info["converged"])


if __name__ == "__main__":
    unittest.main()
