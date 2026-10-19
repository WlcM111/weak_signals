"""R-049 (C-3116): строгий резервный перевод запроса без LLM — флаг WS_QUERY_FALLBACK_STRICT."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from insight.application.use_cases import expand_query
from insight.application.use_cases.expand_query import ExpandQuery, Glossary, normalize
from ws_common.query_fallback import fallback_terms, untranslated_words

ROOT = Path(__file__).resolve().parents[4]
GLOSSARY = ROOT / "services" / "insight" / "config" / "glossary_ru_en.yaml"
# Шесть запросов кейса (tools/theme_analytics.py, THEMES) → переводы, которые должны быть в английской фразе.
CASE_QUERIES = {
    "индустриальный искусственный интеллект на производстве": ("industrial", "artificial intelligence",
                                                               "manufacturing"),
    "робототехника и физический искусственный интеллект": ("robotics", "physical", "artificial intelligence"),
    "инфраструктура для искусственного интеллекта и дата-центры": ("infrastructure", "artificial intelligence",
                                                                   "data center"),
    "перспективные решения в финтехе": ("fintech",),
    "защита и безопасность систем искусственного интеллекта": ("protection", "security", "systems",
                                                               "artificial intelligence"),
    "edge-вычисления и периферийный инференс": ("edge computing", "inference"),
}
# Прежний вывод на словаре insight (замер X32): без флага он не меняется.
LEGACY_EN = {
    "медиа и развлечения": ("medicine",),
    "индустриальный искусственный интеллект на производстве": ("artificial intelligence manufacturing",
                                                               "artificial intelligence"),
    "робототехника и физический искусственный интеллект": ("robotics artificial intelligence",
                                                           "artificial intelligence"),
    "инфраструктура для искусственного интеллекта и дата-центры": ("artificial intelligence",),
    "перспективные решения в финтехе": ("fintech",),
    "защита и безопасность систем искусственного интеллекта": ("security artificial intelligence",),
    "edge-вычисления и периферийный инференс": ("edge",),
}


class _Recorder:
    """Журнал-дублёр: запоминает события и поля."""

    def __init__(self, events: list) -> None:
        self.events = events

    def __getattr__(self, level: str):  # noqa: ANN204
        return lambda event, **fields: self.events.append((level, event, fields))


class StrictFallbackTermsTest(unittest.TestCase):
    """Строгий режим fallback_terms: основа слова, дефис, дополнение словаря, непереведённые слова."""

    def setUp(self) -> None:
        self.terms = Glossary.load(GLOSSARY).strict_terms

    def test_media_is_not_medicine(self) -> None:
        # без статьи «медиа» слово остаётся непереведённым, а не подменяется «медициной»
        only_medicine = {"медицина": "medicine"}
        _, en_terms = fallback_terms("медиа и развлечения", only_medicine, strict=True)
        self.assertNotIn("medicine", " ".join(en_terms))
        self.assertIn("медиа", untranslated_words("медиа и развлечения", only_medicine, strict=True))
        _, en_terms = fallback_terms("медиа и развлечения", self.terms, strict=True)
        self.assertNotIn("medicine", " ".join(en_terms))
        self.assertIn("media entertainment", en_terms)

    def test_inflected_forms_still_match(self) -> None:
        terms = {"ии": "artificial intelligence", "искусственный интеллект": "artificial intelligence",
                 "финтех": "fintech", "кибербезопасность": "cybersecurity", "квантовые": "quantum",
                 "сенсоры": "sensors", "медицина": "medicine"}
        for query, expected in (
            ("перспективные решения в финтехе", ("fintech",)),
            ("слабые сигналы в области кибербезопасности", ("cybersecurity",)),
            ("технологии искусственного интеллекта", ("artificial intelligence",)),
            ("квантовые сенсоры в медицине", ("quantum sensors medicine", "quantum sensors")),
        ):
            with self.subTest(query=query):
                self.assertEqual(fallback_terms(query, terms, strict=True)[1], expected)

    def test_derived_adjectives_keep_translation(self) -> None:
        # производное от длинной основы переводится и в строгом режиме: «медицинские» — medicine, не media
        for query, expected in (
            ("медицинские роботы", "medicine robots"),
            ("финансовые платформы", "finance platforms"),
        ):
            with self.subTest(query=query):
                self.assertEqual(fallback_terms(query, self.terms, strict=True)[1][0], expected)

    def test_hyphenated_word_is_split(self) -> None:
        self.assertEqual(fallback_terms("edge-вычисления", {"вычисления": "computing"}, strict=True)[1],
                         ("edge computing",))

    def test_case_queries_keep_content_words(self) -> None:
        for query, expected in CASE_QUERIES.items():
            with self.subTest(query=query):
                phrase = fallback_terms(query, self.terms, strict=True)[1][0]
                for translation in expected:
                    self.assertIn(translation, phrase)
                self.assertEqual(untranslated_words(query, self.terms, strict=True), ())

    def test_untranslated_words_are_reported(self) -> None:
        query = "квантовые голограммы в медицине"
        _, en_terms = fallback_terms(query, self.terms, strict=True)
        self.assertEqual(en_terms[0], "quantum medicine")
        self.assertEqual(untranslated_words(query, self.terms, strict=True), ("голограммы",))

    def test_russian_terms_are_not_affected(self) -> None:
        for query in CASE_QUERIES:
            with self.subTest(query=query):
                self.assertEqual(fallback_terms(query, self.terms, strict=True)[0],
                                 fallback_terms(query, Glossary.load(GLOSSARY).terms)[0])


class ExpandQueryStrictFlagTest(unittest.TestCase):
    """Проброс флага в резервный путь ExpandQuery."""

    def setUp(self) -> None:
        self.glossary = Glossary.load(GLOSSARY)

    def fallback(self, query: str, **flag: bool):  # noqa: ANN201
        use_case = ExpandQuery(None, None, None, self.glossary, **flag)  # type: ignore[arg-type]
        return use_case.fallback(query, normalize(query))

    def test_flag_on_translates_case_query(self) -> None:
        expansion = self.fallback("инфраструктура для искусственного интеллекта и дата-центры", fallback_strict=True)
        self.assertTrue(expansion.used_fallback)
        joined = " | ".join(expansion.en_terms)
        for translation in ("infrastructure", "artificial intelligence", "data center"):
            self.assertIn(translation, joined)

    def test_flag_on_logs_untranslated_words(self) -> None:
        events: list = []
        with mock.patch.object(expand_query, "log", _Recorder(events)):
            self.fallback("квантовые голограммы", fallback_strict=True)
        self.assertIn(("warning", "expand.fallback_untranslated", {"words": ["голограммы"]}), events)


class FlagOffUnchangedTest(unittest.TestCase):
    """Без флага — прежний вывод: дополнение словаря и строгие правила не действуют."""

    def test_legacy_output_on_extended_glossary(self) -> None:
        terms = Glossary.load(GLOSSARY).terms
        for query, expected in LEGACY_EN.items():
            with self.subTest(query=query):
                self.assertEqual(fallback_terms(query, terms)[1], expected)

    def test_expand_query_fallback_without_flag(self) -> None:
        use_case = ExpandQuery(None, None, None, Glossary.load(GLOSSARY))  # type: ignore[arg-type]
        events: list = []
        with mock.patch.object(expand_query, "log", _Recorder(events)):
            expansion = use_case.fallback("медиа и развлечения", normalize("медиа и развлечения"))
            use_case.fallback("квантовые голограммы", normalize("квантовые голограммы"))
        self.assertEqual(expansion.en_terms, ("medicine",))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
