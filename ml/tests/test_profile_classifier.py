"""Классификатор по профилям: проверка по темам против рубрики и решение об использовании."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from ml import profile_classifier


class ProfileClassifierTest(unittest.TestCase):
    def test_decision_compares_with_rubric_by_topic(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            rows, preds = [], []
            for i in range(60):
                positive = i % 3 == 0
                gid = f"bgrp-{i}"
                rows.append({"group_id": gid, "label": int(positive), "topic": f"t{i % 6}", "title": f"Карточка {i}"})
                preds.append({"id": gid, "pred_code": "U", "confidence": 0.5, "technology_ru": f"Технология {i}",
                              "profile_ru": "Первые пилотные партии, стартапы после раундов." if positive
                              else "Массовое внедрение, отраслевой стандарт."})
            (base / "dataset_b_v2.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            (base / "dataset_b_v2_uncertain.jsonl").write_text("", encoding="utf-8")
            with (base / "p.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(preds[0]))
                writer.writeheader()
                writer.writerows(preds)
            code = profile_classifier.main(["--data-dir", str(base), "--predictions", str(base / "p.csv"),
                                            "--out", str(base / "m.json")])
            self.assertEqual(code, 0)
            out = json.loads((base / "m.json").read_text(encoding="utf-8"))
            self.assertTrue(out["decision"]["use_as_scorer"])  # рубрика всем дала U — модель лучше
            self.assertEqual(out["validation"]["rows"], 60)


if __name__ == "__main__":
    unittest.main()
