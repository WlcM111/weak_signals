"""Чтение датасета организаторов, очистка от утечки и сборка негативов."""

from __future__ import annotations

import unittest
from pathlib import Path

from ml.dataset import (
    clean_description,
    load_leak_patterns,
    ordinal_from,
    parse_sources,
    read_positive_rows,
    slugify,
    validate_rows,
)
from ml.negatives import cohen_kappa, load_negatives
from ml.leak_check import run_leak_check

ROOT = Path(__file__).resolve().parents[2]
CSV = ROOT / "ml" / "data" / "raw" / "dataset_normalized.csv"
LEAK = ROOT / "ml" / "leak_patterns.yaml"
SCHEMA = ROOT / "schemas" / "training_row.schema.json"
NEGATIVES = ROOT / "ml" / "data" / "labels" / "negatives_dev_b.yaml"


class PositiveRowsTest(unittest.TestCase):
    """Сто позитивов датасета читаются и соответствуют профилю."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = read_positive_rows(CSV, LEAK)

    def test_row_count(self) -> None:
        self.assertEqual(len(self.rows), 100)

    def test_row_ids_and_labels(self) -> None:
        self.assertEqual(self.rows[0].row_id, "pos-0001")
        self.assertTrue(all(row.label == 1 for row in self.rows))
        self.assertTrue(all(row.label_kind == "WEAK_SIGNAL" for row in self.rows))

    def test_domains_match_profile(self) -> None:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.domain_tag] = counts.get(row.domain_tag, 0) + 1
        self.assertEqual(
            counts,
            {
                "industrial_ai": 17,
                "ai_infrastructure": 17,
                "robotics": 17,
                "fintech": 17,
                "edge": 16,
                "ai_security": 16,
            },
        )

    def test_stage_distribution_matches_profile(self) -> None:
        counts: dict[int, int] = {}
        for row in self.rows:
            counts[row.stage_ordinal] = counts.get(row.stage_ordinal, 0) + 1
        self.assertEqual(counts, {1: 20, 2: 35, 3: 25, 4: 20})

    def test_every_row_has_sources(self) -> None:
        self.assertTrue(all(row.source_urls for row in self.rows))

    def test_schema_valid(self) -> None:
        self.assertEqual(validate_rows(self.rows, SCHEMA), [])

    def test_markers_removed_from_descriptions(self) -> None:
        joined = " ".join(row.description.lower() for row in self.rows)
        for marker in ("слабый сигнал", "растёт быстро", "тренд упоминаний"):
            self.assertNotIn(marker, joined)


class CleaningTest(unittest.TestCase):
    """Очистка описаний и разбор вспомогательных колонок."""

    def test_removes_leak_phrases(self) -> None:
        patterns, _ = load_leak_patterns(LEAK)
        text = "Это слабый сигнал: зарождающийся рынок, растёт быстро с 2026 года."
        cleaned = clean_description(text, patterns).lower()
        self.assertNotIn("слабый сигнал", cleaned)
        self.assertNotIn("растёт быстро", cleaned)
        self.assertIn("рынок", cleaned)

    def test_markdown_links_become_text(self) -> None:
        patterns, _ = load_leak_patterns(LEAK)
        self.assertEqual(clean_description("[Источник](https://a.b/c) важен", patterns), "Источник важен")

    def test_parse_sources(self) -> None:
        value = "[A](https://a.example/1), [B](https://b.example/2)"
        self.assertEqual(parse_sources(value), ["https://a.example/1", "https://b.example/2"])

    def test_parse_bare_urls(self) -> None:
        self.assertEqual(parse_sources("см. https://c.example/3."), ["https://c.example/3"])

    def test_ordinal_takes_last_stage(self) -> None:
        self.assertEqual(ordinal_from("Пилот → Раннее внедрение", (("раннее внедрение", 4), ("пилот", 3))), 4)

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Нейроморфные чипы для Edge AI"), "neyromorfnye-chipy-dlya-edge-ai")


class NegativesTest(unittest.TestCase):
    """Список негативов соответствует протоколу §6.2."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = load_negatives(NEGATIVES)

    def test_minimum_count(self) -> None:
        self.assertGreaterEqual(len(self.report.rows), 120)

    def test_all_six_domains_covered(self) -> None:
        self.assertEqual(len(self.report.by_domain), 6)
        self.assertTrue(all(count >= 15 for count in self.report.by_domain.values()))

    def test_subtypes_present(self) -> None:
        self.assertEqual(set(self.report.by_subtype), {"mature", "hype", "noise"})

    def test_label_kinds(self) -> None:
        kinds = {row.label_kind for row in self.report.rows}
        self.assertEqual(kinds, {"MATURE", "HYPE_OR_NOISE"})

    def test_all_negative_labels(self) -> None:
        self.assertTrue(all(row.label == 0 for row in self.report.rows))

    def test_sources_present(self) -> None:
        self.assertTrue(all(row.source_urls for row in self.report.rows))

    def test_schema_valid(self) -> None:
        self.assertEqual(validate_rows(list(self.report.rows), SCHEMA), [])

    def test_cross_annotation_warning_is_reported(self) -> None:
        self.assertTrue(any("каппа" in warning for warning in self.report.warnings))


class KappaTest(unittest.TestCase):
    """Каппа Коэна считается корректно."""

    def test_full_agreement(self) -> None:
        self.assertAlmostEqual(cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]), 1.0)

    def test_partial_agreement(self) -> None:
        value = cohen_kappa([1, 1, 0, 0, 1, 0], [1, 0, 0, 0, 1, 0])
        self.assertGreater(value, 0.0)
        self.assertLess(value, 1.0)

    def test_no_agreement(self) -> None:
        self.assertLess(cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1]), 0.0)


class LeakCheckTest(unittest.TestCase):
    """Контроль утечки ловит маркеры и пропускает очищенные тексты."""

    def test_clean_dataset_passes(self) -> None:
        rows = read_positive_rows(CSV, LEAK)
        _, markers = load_leak_patterns(LEAK)
        negatives = list(load_negatives(NEGATIVES).rows)
        result = run_leak_check([*rows, *negatives], markers, seed=20260915)
        self.assertTrue(result.passed, result.summary)

    def test_detects_injected_markers(self) -> None:
        from dataclasses import replace

        rows = read_positive_rows(CSV, LEAK)[:60]
        negatives = list(load_negatives(NEGATIVES).rows)[:60]
        poisoned = [replace(row, description=row.description + " слабый сигнал зарождающийся тренд") for row in rows]
        result = run_leak_check([*poisoned, *negatives], ["слабый", "сигнал", "зарождающийся", "тренд"], seed=1)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
