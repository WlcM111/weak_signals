"""Регрессионные тесты резервного расширения запроса без LLM (ws_common.query_fallback)."""

from __future__ import annotations

import unittest

from ws_common.query_fallback import content_segments, fallback_terms

TERMS = {
    "ии": "artificial intelligence",
    "искусственный интеллект": "artificial intelligence",
    "финтех": "fintech",
    "кибербезопасность": "cybersecurity",
    "квантовые": "quantum",
    "сенсоры": "sensors",
    "медицина": "medicine",
    "технологии": "technologies",
}


class FallbackTermsTest(unittest.TestCase):
    def test_frame_words_are_dropped(self) -> None:
        self.assertEqual(content_segments("слабые сигналы в области кибербезопасности"), [["кибербезопасности"]])

    def test_inflected_forms_are_translated(self) -> None:
        for query, expected in (
            ("технологии в ИИ", "artificial intelligence"),
            ("перспективные решения в финтехе", "fintech"),
            ("слабые сигналы в области кибербезопасности", "cybersecurity"),
            ("технологии искусственного интеллекта", "artificial intelligence"),
        ):
            with self.subTest(query=query):
                _, en_terms = fallback_terms(query, TERMS)
                self.assertEqual(en_terms, (expected,))

    def test_english_terms_never_contain_cyrillic(self) -> None:
        _, en_terms = fallback_terms("квантовые сенсоры в неизвестнойобласти медицине", TERMS)
        self.assertTrue(en_terms)
        for term in en_terms:
            self.assertNotRegex(term, "[а-яё]")

    def test_multiword_phrase_preferred_over_generic_single_words(self) -> None:
        _, en_terms = fallback_terms("квантовые сенсоры в медицине", TERMS)
        self.assertEqual(en_terms, ("quantum sensors medicine", "quantum sensors"))

    def test_latin_query_passes_through(self) -> None:
        self.assertEqual(fallback_terms("edge AI", TERMS)[1], ("edge ai",))

    def test_untranslatable_query_keeps_original(self) -> None:
        ru_terms, en_terms = fallback_terms("нейроморфные чипы", {})
        self.assertEqual((ru_terms, en_terms), (("нейроморфные чипы",), ("нейроморфные чипы",)))