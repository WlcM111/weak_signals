"""Регрессионные тесты исправлений аудита в ML-конвейере."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from ml.dataset import (
    STAGE_MAP,
    TrainingRow,
    align_text_lengths,
    ordinal_from,
    read_positive_rows,
)
from ml.evaluate import rows_from_table
from ml.leak_check import length_only_accuracy
from ml.negatives import load_negatives

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "ml" / "data" / "raw" / "dataset_normalized.csv"
LEAK = ROOT / "ml" / "leak_patterns.yaml"
NEGATIVES = ROOT / "ml" / "data" / "labels" / "negatives_dev_b.yaml"


def _row(index: int, label: int, description: str) -> TrainingRow:
    return TrainingRow(
        row_id=f"{'pos' if label else 'neg'}-{index:04d}",
        group_id=f"g{index}",
        origin="test",
        title=f"Технология {index}",
        description=description,
        domain_tag="edge",
        label=label,
        label_kind="WEAK_SIGNAL" if label else "MATURE",
    )


class DatasetConstructionTest(unittest.TestCase):
    def test_positive_text_has_no_companies_suffix(self) -> None:
        rows = read_positive_rows(RAW, LEAK)
        self.assertEqual(len(rows), 100)
        self.assertFalse([row.row_id for row in rows if "Компании:" in row.description])

    def test_mass_adoption_is_not_early_adoption(self) -> None:
        self.assertEqual(ordinal_from("Массовое внедрение", STAGE_MAP), 5)
        self.assertEqual(ordinal_from("Раннее внедрение", STAGE_MAP), 4)


class LengthConfoundTest(unittest.TestCase):
    def test_alignment_removes_length_signal_on_real_dataset(self) -> None:
        rows = [*read_positive_rows(RAW, LEAK), *load_negatives(NEGATIVES).rows]
        self.assertGreater(length_only_accuracy(rows, seed=1), 0.85)  # до выравнивания длина выдаёт класс
        aligned = align_text_lengths(rows)
        self.assertLess(length_only_accuracy(aligned, seed=1), 0.75)  # после — близко к доле большего класса
        self.assertEqual([row.label for row in aligned], [row.label for row in rows])
        self.assertEqual([row.title for row in aligned], [row.title for row in rows])

    def test_alignment_truncates_only_the_longer_class(self) -> None:
        rows = [_row(1, 1, "Длинное описание, " * 20), _row(2, 0, "Короткое описание.")]
        aligned = align_text_lengths(rows)
        self.assertLessEqual(len(aligned[0].description), len("Короткое описание."))
        self.assertEqual(aligned[1].description, "Короткое описание.")


class EvaluatePreprocessingTest(unittest.TestCase):
    def test_description_is_cleaned_like_training(self) -> None:
        patterns = [re.compile("слабый сигнал", re.IGNORECASE)]
        records = [{"tech": "Квантовые сенсоры", "why": "Это слабый сигнал: ранние пилоты в клиниках"}]
        rows = rows_from_table(records, patterns)
        self.assertNotIn("слабый сигнал", rows[0].description.lower())
        self.assertIn("ранние пилоты", rows[0].description)