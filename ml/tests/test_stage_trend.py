"""Базовая модель стадии и тренда: отчёт на реальных строках организаторов (планка для рубрики LLM)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ml import stage_trend

DATA = Path(__file__).resolve().parents[1] / "data" / "labels" / "dataset_ds-2026.09.19-v2.jsonl"


@unittest.skipUnless(DATA.is_file(), "нет размеченного датасета A")
class StageTrendBaselineTest(unittest.TestCase):
    def test_report_on_organizer_rows(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(stage_trend.main(["--data", str(DATA), "--out", out]), 0)
            report = json.loads((Path(out) / "stage_trend_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rows"], 100)
        self.assertEqual(report["stage"]["distribution"], {"1": 20, "2": 35, "3": 25, "4": 20})
        self.assertEqual(report["trend"]["distribution"], {"1": 4, "2": 60, "3": 36})
        for target in ("stage", "trend"):
            self.assertTrue(0.0 <= report[target]["model"]["accuracy"] <= 1.0)
            self.assertTrue(0.0 <= report[target]["majority"]["accuracy"] <= 1.0)


if __name__ == "__main__":
    unittest.main()
