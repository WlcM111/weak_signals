"""Признаки реестра v1 на синтетических текстах и документах (§12.7 ТЗ)."""

from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta

from analyzer.domain.features import (
    COLLECTION_FEATURES,
    EncyclopediaSignal,
    Lexicons,
    TermSet,
    collection_features,
    cosine,
    embedding_features,
    empty_collection_features,
    encyclopedia_features,
    lexicon_rate,
    lexical_features,
)
from analyzer.domain.values import SourceType, TrustLevel
from ..fakes import make_document

NOW = datetime(2026, 9, 15, tzinfo=UTC)

LEXICONS = Lexicons(
    emergence=TermSet.from_terms(["прототип*", "пилотн*", "proof of concept"]),
    maturity=TermSet.from_terms(["рынок", "стандарт*", "массов* внедрение"]),
    hype=TermSet.from_terms(["революционн*", "game changer"]),
    bigtech=TermSet.from_terms(["google", "сбер"]),
    stopwords=frozenset({"и", "в", "the"}),
    stage_terms={
        2: TermSet.from_terms(["прототип*"]),
        3: TermSet.from_terms(["пилотн*"]),
        5: TermSet.from_terms(["массов* внедрение"]),
    },
)


class TermSetTest(unittest.TestCase):
    """Разбор лексикона: точные токены, префиксы, фразы."""

    def test_splits_terms_by_kind(self) -> None:
        terms = TermSet.from_terms(["рынок", "прототип*", "массовое внедрение", "  "])
        self.assertEqual(terms.tokens, frozenset({"рынок"}))
        self.assertEqual(terms.prefixes, ("прототип",))
        self.assertEqual(terms.phrases, ("массовое внедрение",))

    def test_counts_tokens_prefixes_and_phrases(self) -> None:
        terms = TermSet.from_terms(["рынок", "прототип*", "массовое внедрение"])
        text = "рынок прототипы прототипа массовое внедрение массовое внедрение"
        tokens = text.split()
        self.assertEqual(terms.count_in(tokens, text), 5)

    def test_prefix_does_not_match_unrelated_word(self) -> None:
        terms = TermSet.from_terms(["пилот*"])
        self.assertEqual(terms.count_in(["пила", "пилигрим"], "пила пилигрим"), 0)


class LexiconRateTest(unittest.TestCase):
    """Шкала лексической плотности: 0 без совпадений, насыщение на 2 % токенов."""

    def test_zero_without_hits(self) -> None:
        self.assertEqual(lexicon_rate(0, 500), 0.0)

    def test_zero_on_empty_text(self) -> None:
        self.assertEqual(lexicon_rate(3, 0), 0.0)

    def test_saturates_at_one(self) -> None:
        self.assertEqual(lexicon_rate(40, 1000), 1.0)

    def test_short_text_uses_minimal_denominator(self) -> None:
        # одно совпадение в тексте из 20 токенов не должно давать максимума
        self.assertAlmostEqual(lexicon_rate(1, 20), 0.25, places=6)

    def test_is_monotonic_in_hits(self) -> None:
        self.assertLess(lexicon_rate(5, 2000), lexicon_rate(10, 2000))


class LexicalFeaturesTest(unittest.TestCase):
    """Восемь лексических признаков."""

    def test_emergence_text(self) -> None:
        text = "Команда собрала прототип и запустила пилотные испытания в 2026 году."
        values = lexical_features(text, LEXICONS, NOW)
        self.assertGreater(values["lex_emergence_score"], 0.0)
        self.assertEqual(values["lex_maturity_score"], 0.0)
        self.assertEqual(values["stage_lex_ordinal"], 3.0)
        self.assertEqual(values["recent_year_share"], 1.0)

    def test_maturity_text_sets_stage_five(self) -> None:
        text = "Стандарт закреплён, началось массовое внедрение на рынке."
        values = lexical_features(text, LEXICONS, NOW)
        self.assertEqual(values["stage_lex_ordinal"], 5.0)
        self.assertGreater(values["lex_maturity_score"], 0.0)

    def test_bigtech_and_funding_counts(self) -> None:
        text = "Google и Сбер вложили $5 млн в раунде; посевной раунд инвестиций закрыт."
        values = lexical_features(text, LEXICONS, NOW)
        self.assertEqual(values["bigtech_mentions_count"], 2.0)
        self.assertGreaterEqual(values["funding_mentions_count"], 1.0)

    def test_recent_year_share_counts_only_plausible_years(self) -> None:
        text = "Работа 1998 года, продолжение в 2019 и 2026."
        values = lexical_features(text, LEXICONS, NOW)
        self.assertAlmostEqual(values["recent_year_share"], 0.5, places=6)

    def test_no_years_gives_zero(self) -> None:
        self.assertEqual(lexical_features("без дат", LEXICONS, NOW)["recent_year_share"], 0.0)


class CollectionFeaturesTest(unittest.TestCase):
    """Одиннадцать коллекционных признаков с известными долями."""

    def setUp(self) -> None:
        self.documents = [
            make_document(1, "A", source_type=SourceType.SCIENTIFIC_PUBLICATION,
                          trust_level=TrustLevel.HIGH, published_at=NOW - timedelta(days=30),
                          citation_count=4),
            make_document(2, "B", source_type=SourceType.PREPRINT, trust_level=TrustLevel.HIGH,
                          published_at=NOW - timedelta(days=200), citation_count=10),
            make_document(3, "C", source_type=SourceType.PRESS_RELEASE, trust_level=TrustLevel.LOW,
                          published_at=NOW - timedelta(days=500)),
            make_document(4, "D", source_type=SourceType.NEWS, trust_level=TrustLevel.MEDIUM,
                          published_at=None),
        ]

    def test_shares_and_counts(self) -> None:
        values = collection_features(self.documents, NOW)
        self.assertAlmostEqual(values["share_scientific"], 0.5)
        self.assertAlmostEqual(values["share_marketing"], 0.25)
        self.assertAlmostEqual(values["share_news_media"], 0.25)
        self.assertAlmostEqual(values["trusted_share"], 0.5)
        self.assertEqual(values["source_type_diversity"], 4.0)
        self.assertAlmostEqual(values["doc_count_log"], math.log1p(4))

    def test_growth_ratio_uses_laplace_smoothing(self) -> None:
        values = collection_features(self.documents, NOW)
        # два документа за последние 12 мес., один — за предыдущие 12
        self.assertAlmostEqual(values["growth_ratio_12m"], 3 / 2)

    def test_citation_median_over_scientific_only(self) -> None:
        values = collection_features(self.documents, NOW)
        self.assertAlmostEqual(values["citation_median_log"], math.log1p(7.0))

    def test_recency_and_first_seen(self) -> None:
        values = collection_features(self.documents, NOW)
        self.assertAlmostEqual(values["recency_median_days"], 200.0, places=6)
        self.assertAlmostEqual(values["first_seen_years_ago"], 500 / 365.25, places=6)

    def test_empty_collection_matches_defaults(self) -> None:
        self.assertEqual(collection_features([], NOW), empty_collection_features())
        self.assertEqual(set(empty_collection_features()), set(COLLECTION_FEATURES))
        self.assertEqual(empty_collection_features()["growth_ratio_12m"], 1.0)


class EncyclopediaFeaturesTest(unittest.TestCase):
    """Три энциклопедических признака."""

    def test_absent_article(self) -> None:
        values = encyclopedia_features(EncyclopediaSignal(exists=False), NOW)
        self.assertEqual(values, {"wiki_exists": 0.0, "wiki_pageviews_30d_log": 0.0, "wiki_age_years": 0.0})

    def test_present_article(self) -> None:
        signal = EncyclopediaSignal(
            exists=True, pageviews_30d=45000, created_at=NOW - timedelta(days=int(365.25 * 7))
        )
        values = encyclopedia_features(signal, NOW)
        self.assertEqual(values["wiki_exists"], 1.0)
        self.assertAlmostEqual(values["wiki_pageviews_30d_log"], math.log1p(45000))
        self.assertAlmostEqual(values["wiki_age_years"], 7.0, places=2)

    def test_unavailable_pageviews_are_zero(self) -> None:
        values = encyclopedia_features(EncyclopediaSignal(exists=True, pageviews_30d=-1), NOW)
        self.assertEqual(values["wiki_pageviews_30d_log"], 0.0)


class EmbeddingFeaturesTest(unittest.TestCase):
    """Косинусные признаки и защита от отсутствующих векторов."""

    def test_cosine_of_identical_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [2.0, 0.0]), 1.0)

    def test_cosine_of_orthogonal_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_missing_vector_gives_zero(self) -> None:
        self.assertEqual(cosine([1.0, 0.0], None), 0.0)

    def test_embedding_features_keys(self) -> None:
        values = embedding_features([1.0, 0.0], [1.0, 0.0], [0.0, 1.0], None)
        self.assertAlmostEqual(values["emb_sim_weak_centroid"], 1.0)
        self.assertAlmostEqual(values["emb_sim_mature_centroid"], 0.0)
        self.assertEqual(values["emb_sim_query"], 0.0)


if __name__ == "__main__":
    unittest.main()
