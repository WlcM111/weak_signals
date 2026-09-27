"""Локальная модель слабого сигнала: признаки, предсказание, вклады и обучение с проверкой по темам."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from ml import rubric_ranker
from ws_common import rubric_model

SHIPPED = Path(__file__).resolve().parents[2] / "services" / "orchestrator" / "config" / "rubric_ranker.json"


def feats(code: str, types: list[str], titles: list[str] | None = None) -> dict[str, float]:
    return rubric_model.features(code, 0.9, 3, 3, types, ["HIGH"] * len(types), titles or ["t"] * len(types),
                                 [2025] * len(types), 0.6)


class RubricModelTest(unittest.TestCase):
    def test_features(self) -> None:
        values = feats("R", ["SCIENTIFIC_PUBLICATION", "PATENT", "CODE_REPOSITORY", "INDUSTRY_MEDIA"],
                       ["A review of X", "p", "c", "m"])
        self.assertEqual((values["code_R"], values["patent_share"], values["market_share"]), (1.0, 0.25, 0.25))
        self.assertEqual((values["independent_share"], values["review_share"]), (0.75, 0.25))

    @unittest.skipUnless(SHIPPED.is_file(), "в образе trainer нет services/orchestrator: модель проверяется на хосте")
    def test_shipped_model_prefers_signal_over_overview(self) -> None:
        model = rubric_model.load(SHIPPED)
        self.assertIsNotNone(model)
        good = rubric_model.predict(model, feats("R", ["SCIENTIFIC_PUBLICATION", "INDUSTRY_MEDIA"]))
        overview = rubric_model.predict(model, feats("N-OVR", ["SCIENTIFIC_PUBLICATION"], ["A survey of X"]))
        self.assertGreater(good, overview)
        self.assertTrue(0.0 < overview < good < 1.0)
        top = rubric_model.contributions(model, feats("N-OVR", ["SCIENTIFIC_PUBLICATION"], ["A survey"]))
        self.assertEqual(len(top), len(rubric_model.FEATURES))

    def test_load_rejects_wrong_structure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "m.json"
            bad.write_text(json.dumps({"features": ["x"], "coef": [1], "mean": [0], "scale": [1], "intercept": 0}))
            self.assertIsNone(rubric_model.load(bad))
            self.assertIsNone(rubric_model.load(Path(folder) / "нет.json"))

    def test_ranker_trains_and_reports_cv_by_topic(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            rows, preds = [], []
            for index in range(60):
                positive = index % 3 == 0
                gid = f"bgrp-{index}"
                refs = [{"source_type": "SCIENTIFIC_PUBLICATION" if positive else "PATENT", "trust_level": "HIGH",
                         "title": "Method" if positive else "A review", "published_at": "2025-01-01"}]
                rows.append({"group_id": gid, "label": int(positive), "topic": f"t{index % 6}",
                             "evidence_refs": refs, "observed_runtime": {"score": 0.5}})
                preds.append({"id": gid, "pred_code": "R" if positive else "N-OVR", "stage_pred": 2,
                              "trend_pred": 2, "confidence": 0.9})
            (base / "dataset_b_v2.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            (base / "dataset_b_v2_uncertain.jsonl").write_text("", encoding="utf-8")
            with (base / "p.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(preds[0]))
                writer.writeheader()
                writer.writerows(preds)
            self.assertEqual(rubric_ranker.main(["--data-dir", str(base), "--predictions", str(base / "p.csv"),
                                                 "--out", str(base / "m.json")]), 0)
            model = rubric_model.load(base / "m.json")
            self.assertIsNotNone(model)
            self.assertEqual(model["cv_by_topic"]["local_model"]["roc_auc"], 1.0)


if __name__ == "__main__":
    unittest.main()
