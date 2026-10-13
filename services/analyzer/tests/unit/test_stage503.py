"""Этап 5.0.3: проверка Wikipedia — по языку письма названия, пачками по 10."""

from __future__ import annotations

import unittest

from analyzer.application.use_cases.run_analysis import _CYRILLIC, MAX_ENCYCLOPEDIA_TITLES


class EncyclopediaLanguageTest(unittest.TestCase):
    def test_script_selects_language_and_batches_are_small(self) -> None:
        self.assertIsNone(_CYRILLIC.search("federated learning for factories"))
        self.assertIsNotNone(_CYRILLIC.search("Федеративное обучение"))
        self.assertLessEqual(MAX_ENCYCLOPEDIA_TITLES, 10)


if __name__ == "__main__":
    unittest.main()
