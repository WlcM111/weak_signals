"""Одиночные документы отраслевых СМИ: остаются кандидатами только при keep_single_types (рыночные сигналы)."""

from __future__ import annotations

import unittest

from analyzer.domain.clustering import split_small_clusters
from analyzer.domain.entities import DocumentRef
from analyzer.domain.values import SourceType, TrustLevel


def doc(kind: SourceType, trust: TrustLevel) -> DocumentRef:
    return DocumentRef(document_id=f"d-{kind.value}-{trust.value}", title="t", text="x", language_code="en",
                       source_type=kind, trust_level=trust, origin_domain="site.org")


class MarketSingletonsTest(unittest.TestCase):
    def test_media_singleton_dropped_by_default_and_kept_when_enabled(self) -> None:
        docs = [doc(SourceType.INDUSTRY_MEDIA, TrustLevel.MEDIUM), doc(SourceType.CODE_REPOSITORY, TrustLevel.MEDIUM),
                doc(SourceType.SCIENTIFIC_PUBLICATION, TrustLevel.HIGH)]
        clusters = [[0], [1], [2]]
        kept, misc = split_small_clusters(clusters, docs, 2)
        self.assertEqual((kept, misc), ([[2]], [0, 1]))
        kept, misc = split_small_clusters(clusters, docs, 2, frozenset({SourceType.INDUSTRY_MEDIA, SourceType.NEWS}))
        self.assertEqual((kept, misc), ([[0], [2]], [1]))




class MarketReserveTest(unittest.TestCase):
    def test_reserved_slots_take_market_clusters(self) -> None:
        from analyzer.domain.clustering import select_top_clusters

        clusters = [[0, 1, 2, 3], [4, 5, 6], [7, 8], [9]]
        relevances = [0.9] * 10
        self.assertEqual(select_top_clusters(clusters, relevances, 2), [[0, 1, 2, 3], [4, 5, 6]])
        mask = [False, False, False, True]
        self.assertEqual(select_top_clusters(clusters, relevances, 2, None, mask, 1), [[0, 1, 2, 3], [9]])
        self.assertEqual(select_top_clusters(clusters, relevances, 2, None, mask, 0), [[0, 1, 2, 3], [4, 5, 6]])


if __name__ == "__main__":
    unittest.main()


class PhraseRelevanceTest(unittest.TestCase):
    def test_max_takes_nearest_phrase_mean_blurs(self) -> None:
        import numpy as np

        from analyzer.application.use_cases.run_analysis import phrase_relevance

        phrases = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        mean = phrases.mean(axis=0) / np.linalg.norm(phrases.mean(axis=0))
        niche = np.array([[0.0, 1.0]], dtype=np.float32)
        self.assertAlmostEqual(float(phrase_relevance(niche, mean, phrases, "max")[0]), 1.0, places=5)
        self.assertAlmostEqual(float(phrase_relevance(niche, mean, phrases, "mean")[0]), 0.7071, places=3)
        self.assertAlmostEqual(float(phrase_relevance(niche, mean, None, "max")[0]), 0.7071, places=3)


class MarketGlueTest(unittest.TestCase):
    def test_one_market_note_per_cluster_rest_become_singletons(self) -> None:
        import numpy as np

        from analyzer.domain.clustering import split_market_glue

        docs = [doc(SourceType.SCIENTIFIC_PUBLICATION, TrustLevel.HIGH), doc(SourceType.INDUSTRY_MEDIA, TrustLevel.MEDIUM),
                doc(SourceType.INDUSTRY_MEDIA, TrustLevel.MEDIUM), doc(SourceType.NEWS, TrustLevel.MEDIUM)]
        vectors = np.array([[1.0, 0.0], [0.9, 0.1], [0.1, 0.9], [0.0, 1.0]], dtype=np.float32)
        result = split_market_glue([[0, 1, 2, 3]], docs, vectors, frozenset({SourceType.INDUSTRY_MEDIA, SourceType.NEWS}))
        self.assertEqual(result[0], [0, 1])  # статья и ближайшая к центру заметка
        self.assertEqual(sorted(result[1:]), [[2], [3]])
        self.assertEqual(split_market_glue([[0, 1]], docs, vectors, frozenset({SourceType.INDUSTRY_MEDIA})), [[0, 1]])
