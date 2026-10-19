"""R-043 (COL-07): фильтр отбора технологий сравнивает фразы по словам, а не по подстрокам символов (за флагом)."""

from __future__ import annotations

import unittest
from datetime import date

from collector.domain.emergence import emerging_terms
from collector.domain.technology_mining import mine_technologies, words_within


def titles_with(phrases: list[str]) -> list[tuple[str, str]]:
    rows = []
    for number, phrase in enumerate(phrases):
        rows.append((f"Startup shows {phrase} prototype", f"source-a{number}"))
        rows.append((f"New results on {phrase} in labs", f"source-b{number}"))
    rows += [(f"Unrelated report number {n} about markets", f"filler{n}") for n in range(3 * len(phrases))]
    return rows


class WordLevelBlockingTest(unittest.TestCase):
    def test_words_within(self) -> None:
        self.assertTrue(words_within("machine learning", "quantum machine learning"))
        self.assertFalse(words_within("ai security", "genai security"))

    def test_narrow_technologies_are_not_blocked_by_generic_substrings(self) -> None:
        phrases = ["quantum machine learning", "orbital data center"]  # обрывались общими «machine learning», «data center»
        mined = mine_technologies(titles_with(phrases), exclude=(), limit=15, word_level=True)
        for phrase in phrases:
            self.assertIn(phrase, mined)

    def test_topic_phrase_blocks_by_words_not_characters(self) -> None:
        mined = mine_technologies(titles_with(["genai security", "ai security"]), exclude=("ai security",), limit=15,
                                  word_level=True)
        self.assertIn("genai security", mined)
        self.assertNotIn("ai security", mined)

    def test_flag_off_keeps_previous_behaviour(self) -> None:
        mined = mine_technologies(titles_with(["quantum machine learning", "genai security"]), exclude=("ai security",),
                                  limit=15)
        self.assertNotIn("quantum machine learning", mined)
        self.assertNotIn("genai security", mined)

    def test_emerging_terms_topic_phrase_by_words(self) -> None:
        rows = [(f"genai security pilot {n}", date(2026, 9, 1)) for n in range(6)]
        rows += [(f"older market note {n}", date(2025, 3, 1)) for n in range(30)]
        args = dict(recent_from=date(2026, 6, 1), base_from=date(2025, 1, 1), base_to=date(2026, 5, 31),
                    exclude=("ai security",), min_recent=3, max_share=1.0)
        on = [item.term for item in emerging_terms(rows, word_level=True, **args)]
        off = [item.term for item in emerging_terms(rows, **args)]
        self.assertTrue(any("genai security" in term for term in on))
        self.assertFalse(any("genai security" in term for term in off))


class NestedAbsorptionByWordsTest(unittest.TestCase):
    """R-043, продолжение (CONTINUE №1): вложенная фраза поглощается только при совпадении по словам."""

    def test_rag_pipelines_survives_next_to_graphrag_pipelines(self) -> None:
        titles = titles_with(["graphrag pipelines", "graphrag pipelines", "rag pipelines"])
        on = mine_technologies(titles, exclude=(), limit=15, word_level=True)
        self.assertIn("graphrag pipelines", on)
        self.assertIn("rag pipelines", on)  # по подстрокам «rag pipelines» ⊂ «graphrag pipelines» — пропадала
        off = mine_technologies(titles, exclude=(), limit=15)
        self.assertNotIn("rag pipelines", off)  # прежнее поведение без флага

    def test_true_nested_phrase_is_still_absorbed(self) -> None:
        titles = titles_with(["machine learning", "quantum machine learning", "quantum machine learning"])
        mined = mine_technologies(titles, exclude=(), limit=15, word_level=True)
        self.assertNotIn("machine learning", mined)  # реже длинной фразы и вложена по словам — поглощена

    def test_emerging_nested_terms_by_words(self) -> None:
        rows = [(f"graphrag pipelines launch {n}", date(2026, 9, 1)) for n in range(8)]
        rows += [(f"rag pipelines deployment {n}", date(2026, 9, 1)) for n in range(5)]
        rows += [(f"older market note {n}", date(2025, 3, 1)) for n in range(30)]
        args = dict(recent_from=date(2026, 6, 1), base_from=date(2025, 1, 1), base_to=date(2026, 5, 31),
                    exclude=(), min_recent=3, max_share=1.0)
        on = [item.term for item in emerging_terms(rows, word_level=True, **args)]
        self.assertTrue(any(term.startswith("rag pipelines") for term in on), on)


if __name__ == "__main__":
    unittest.main()
