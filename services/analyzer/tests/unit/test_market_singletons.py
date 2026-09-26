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


if __name__ == "__main__":
    unittest.main()
