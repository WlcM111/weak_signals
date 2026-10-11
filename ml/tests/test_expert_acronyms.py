"""Этап 4.3: короткие названия сигналов учитываются отдельным словом в верхнем регистре."""

from __future__ import annotations

import unittest

from ml.benchmark.expert_match import acronym_hit, expert_acronyms


class ExpertAcronymTest(unittest.TestCase):
    def test_acronyms_from_title_and_synonyms(self) -> None:
        row = {"row_id": "pos-0009", "title": "Оптическая коммутация (OCS, optical circuit switching)"}
        self.assertEqual(expert_acronyms(row, {"pos-0009": ["ocs", "optical switching"]}), ["OCS"])
        self.assertEqual(expert_acronyms({"row_id": "x", "title": "НПЗ на ИИ (НПЗ)"}, {}), ["НПЗ"])

    def test_acronym_needs_signal_context(self) -> None:
        from ml.benchmark.expert_match import expert_context

        context = expert_context({"row_id": "pos-0009"}, {"pos-0009": ["optical circuit switching", "photonic switch fabric"]})
        self.assertEqual(acronym_hit("OCS fabric for AI clusters", ["OCS"], context), "OCS")
        self.assertIsNone(acronym_hit("OCS announced quarterly earnings", ["OCS"], context))  # этап 4.6, аудит P01

    def test_arxiv_html_and_tracking_parameters(self) -> None:
        from ml.benchmark.expert_match import url_keys

        self.assertIn("arxiv:2604.24447", url_keys("https://arxiv.org/html/2604.24447v1"))
        self.assertIn("arxiv:2412.02410", url_keys("https://ar5iv.labs.arxiv.org/html/2412.02410"))
        self.assertNotEqual(url_keys("https://example.com/article?id=1"), url_keys("https://example.com/article?id=2"))
        self.assertEqual(url_keys("https://example.com/a?utm_source=x"), url_keys("https://www.example.com/a/"))

    def test_whole_word_upper_case_only(self) -> None:
        self.assertEqual(acronym_hit("Startup ships OCS for AI clusters", ["OCS"]), "OCS")
        self.assertIsNone(acronym_hit("docs and ocs-like terms", ["OCS"]))
        self.assertIsNone(acronym_hit("DOCS are ready", ["OCS"]))


if __name__ == "__main__":
    unittest.main()
