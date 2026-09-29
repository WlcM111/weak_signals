"""Локальный классификатор слабого сигнала: признаки, поставленная модель, обучение с проверкой."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ml import signal_classifier_train
from ws_common import signal_classifier as sc

SHIPPED = Path(__file__).resolve().parents[2] / "services" / "orchestrator" / "config" / "signal_classifier.json"


class SignalClassifierTest(unittest.TestCase):
    def test_years_function_words_and_source_words_are_not_features(self) -> None:
        keys = sc.features("Препринт 2026 года для систем: первые пилотные испытания")
        self.assertFalse(any(k.isdigit() or k in ("для", "препри", "систем") for k in keys))
        self.assertIn("пилотн_испыта", keys)

    def test_negation_is_kept(self) -> None:
        # аудит 29.09: «массовое внедрение» и «нет массового внедрения» давали одинаковые признаки
        self.assertNotEqual(set(sc.features("Массовое внедрение")), set(sc.features("Нет массового внедрения")))
        self.assertIn("не_массов", sc.features("Нет массового внедрения"))
        self.assertTrue({"не_массов", "не_внедре"} <= set(sc.features("Стартапы есть, массового внедрения нет")))
        self.assertIn("старта", sc.features("Стартапы есть, массового внедрения нет"))

    def test_features_are_length_normalized(self) -> None:
        short, long = sc.features("Пилотные испытания"), sc.features("Пилотные испытания прототипа на заводе в 2026 году")
        self.assertAlmostEqual(sum(v * v for v in short.values()), 1.0)
        self.assertAlmostEqual(sum(v * v for v in long.values()), 1.0)
        self.assertIn("пилотн_испыта", short)

    @unittest.skipUnless(SHIPPED.is_file(), "в образе trainer нет services/orchestrator: модель проверяется на хосте")
    def test_shipped_model_separates_early_from_mature(self) -> None:
        model = sc.load(SHIPPED)
        early = sc.predict(model, "Твердотельные электролиты. Первые пилотные партии в 2026 году, стартапы привлекли "
                                  "раунды, серийного производства нет.")
        mature = sc.predict(model, "Облачные ERP-системы. Массовое внедрение, стандарт отрасли, рынок сформирован, "
                                   "выраженные лидеры.")
        self.assertGreater(early, 0.5)
        self.assertLess(mature, 0.5)
        self.assertTrue(sc.contributions(model, "Массовое внедрение, стандарт отрасли"))

    def test_training_reports_validation_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            own = [{"title": f"Технология {i}", "description": ("Первые пилоты и прототипы, стартапы после раундов."
                    if i % 2 else "Массовое внедрение, отраслевой стандарт, рынок сформирован."), "label": i % 2}
                   for i in range(40)]
            org = [dict(r, title=f"Орг {i}") for i, r in enumerate(own[:30])]
            for name, rows in (("own.jsonl", own), ("org.jsonl", org)):
                (base / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            code = signal_classifier_train.main(["--own", str(base / "own.jsonl"), "--organizers", str(base / "org.jsonl"),
                                                 "--out", str(base / "m.json"), "--report", str(base / "r.json")])
            self.assertEqual(code, 0)
            model = sc.load(base / "m.json")
            self.assertEqual(model["validation"]["organizers_cv"]["accuracy"], 1.0)
            self.assertIsNone(sc.load(base / "r.json"))


if __name__ == "__main__":
    unittest.main()
