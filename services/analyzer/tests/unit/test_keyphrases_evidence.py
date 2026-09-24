"""Ключевые фразы с MMR, отбор доказательств и сниппеты (§12.5, шаги 6 и 11)."""

from __future__ import annotations

import unittest

import numpy as np

from analyzer.domain.evidence import build_snippet, select_evidence
from analyzer.domain.keyphrases import candidate_phrases, fallback_title, select_by_mmr
from analyzer.domain.values import TrustLevel
from ..fakes import make_document

STOPWORDS = frozenset({"и", "в", "для", "the", "of", "a"})


class CandidatePhrasesTest(unittest.TestCase):
    """N-граммы 1..3 из заголовков без стоп-слов по краям."""

    def test_extracts_frequent_phrases(self) -> None:
        titles = [
            "Нейроморфные чипы для энергоэффективных вычислений",
            "Нейроморфные чипы в промышленности",
            "Прототип нейроморфного чипа",
        ]
        phrases = candidate_phrases(titles, STOPWORDS)
        self.assertIn("Нейроморфные чипы", phrases)
        self.assertTrue(all(not phrase.lower().startswith("для ") for phrase in phrases))

    def test_rejects_edge_stopwords(self) -> None:
        phrases = [phrase.lower() for phrase in candidate_phrases(["память и вычисления"], STOPWORDS)]
        self.assertNotIn("память и", phrases)
        self.assertNotIn("и вычисления", phrases)
        self.assertIn("память и вычисления", phrases)

    def test_ignores_short_and_numeric_only(self) -> None:
        self.assertEqual(candidate_phrases(["2026 1 5"], STOPWORDS), [])

    def test_empty_titles(self) -> None:
        self.assertEqual(candidate_phrases([], STOPWORDS), [])


class MmrTest(unittest.TestCase):
    """MMR: первым идёт ближайшая к центроиду фраза, дальше — разнообразие."""

    def test_selects_closest_first(self) -> None:
        phrases = ["далёкая", "близкая", "почти близкая"]
        vectors = np.asarray(
            [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.99, 0.01, 0.0]], dtype=np.float32
        )
        target = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        selected = select_by_mmr(phrases, vectors, target, top_k=2)
        self.assertEqual(selected[0], "близкая")

    def test_diversity_prefers_different_phrase(self) -> None:
        # λ=0.7: дубль первой фразы даёт 0.7·0.90 − 0.3·1.00 = 0.33,
        # менее близкая, но непохожая фраза — 0.7·0.80 − 0.3·0.72 = 0.344 и побеждает.
        phrases = ["первая", "дубль первой", "другая тема"]
        vectors = np.asarray(
            [[0.9, 0.436, 0.0], [0.9, 0.436, 0.0], [0.8, 0.0, 0.6]], dtype=np.float32
        )
        target = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        selected = select_by_mmr(phrases, vectors, target, top_k=2, lambda_=0.7)
        self.assertEqual(selected, ["первая", "другая тема"])

    def test_top_k_limit_and_empty(self) -> None:
        vectors = np.eye(3, dtype=np.float32)
        target = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        self.assertEqual(len(select_by_mmr(["a", "b", "c"], vectors, target, top_k=2)), 2)
        self.assertEqual(select_by_mmr([], np.zeros((0, 3), dtype=np.float32), target, 5), [])

    def test_fallback_title(self) -> None:
        self.assertEqual(fallback_title(["", "  Заголовок  "]), "Заголовок")
        self.assertEqual(fallback_title([]), "Без названия")


class SelectEvidenceTest(unittest.TestCase):
    """Доказательства: топ по близости с гарантией HIGH/MEDIUM."""

    def test_takes_top_by_similarity(self) -> None:
        documents = [make_document(index, "d") for index in range(5)]
        similarities = [0.1, 0.9, 0.5, 0.7, 0.3]
        self.assertEqual(select_evidence(documents, similarities, 3), [1, 3, 2])

    def test_guarantees_trusted_document(self) -> None:
        documents = [
            make_document(0, "low-1", trust_level=TrustLevel.LOW),
            make_document(1, "low-2", trust_level=TrustLevel.LOW),
            make_document(2, "trusted", trust_level=TrustLevel.MEDIUM),
        ]
        selected = select_evidence(documents, [0.9, 0.8, 0.1], max_items=2)
        self.assertIn(2, selected)
        self.assertEqual(len(selected), 2)

    def test_no_trusted_documents_at_all(self) -> None:
        documents = [make_document(index, "d", trust_level=TrustLevel.LOW) for index in range(3)]
        self.assertEqual(select_evidence(documents, [0.9, 0.5, 0.1], 2), [0, 1])

    def test_limits_and_empty(self) -> None:
        documents = [make_document(0, "d")]
        self.assertEqual(select_evidence(documents, [0.5], 0), [])
        self.assertEqual(select_evidence([], [], 8), [])


class SnippetTest(unittest.TestCase):
    """Сниппет: первые предложения с ключевой фразой, не длиннее 600 символов."""

    def test_starts_from_sentence_with_keyphrase(self) -> None:
        text = "Вводное предложение. Нейроморфный чип показал результат. Заключение."
        snippet = build_snippet(text, ["нейроморфный чип"])
        self.assertTrue(snippet.startswith("Нейроморфный чип"))

    def test_falls_back_to_beginning(self) -> None:
        text = "Первое предложение. Второе предложение."
        self.assertTrue(build_snippet(text, ["отсутствующая фраза"]).startswith("Первое"))

    def test_length_limit(self) -> None:
        text = "Слово. " * 400
        self.assertLessEqual(len(build_snippet(text, ["слово"])), 600)

    def test_long_single_sentence_is_truncated(self) -> None:
        text = "а" * 900
        snippet = build_snippet(text, [])
        self.assertEqual(len(snippet), 600)

    def test_empty_text(self) -> None:
        self.assertEqual(build_snippet("   ", ["x"]), "")


if __name__ == "__main__":
    unittest.main()
