"""Этап 4.3: короткие названия сигналов учитываются отдельным словом в верхнем регистре."""

from __future__ import annotations

import unittest

from ml.benchmark.expert_match import acronym_hit, expert_acronyms


class ExpertAcronymTest(unittest.TestCase):
    def test_acronyms_from_title_and_synonyms(self) -> None:
        row = {"row_id": "pos-0009", "title": "Оптическая коммутация (OCS, optical circuit switching)"}
        self.assertEqual(expert_acronyms(row, {"pos-0009": ["ocs", "optical switching"]}), ["OCS"])
        self.assertEqual(expert_acronyms({"row_id": "x", "title": "НПЗ на ИИ (НПЗ)"}, {}), ["НПЗ"])

    def test_whole_word_upper_case_only(self) -> None:
        self.assertEqual(acronym_hit("Startup ships OCS for AI clusters", ["OCS"]), "OCS")
        self.assertIsNone(acronym_hit("docs and ocs-like terms", ["OCS"]))
        self.assertIsNone(acronym_hit("DOCS are ready", ["OCS"]))


if __name__ == "__main__":
    unittest.main()
