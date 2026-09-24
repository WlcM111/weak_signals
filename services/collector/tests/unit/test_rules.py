"""Правила нормализации документов (§6 HANDOFF): URL, хеши, язык, токены, page_token."""

from __future__ import annotations

import unittest

from collector.domain.rules import (
    MAX_RAW_META_BYTES,
    canonical_url,
    clean_whitespace,
    content_hash,
    decode_page_token,
    detect_language,
    encode_page_token,
    normalize_doi,
    normalize_encyclopedia_title,
    normalize_language_code,
    normalize_text,
    origin_domain,
    sanitize_raw_meta,
    term_coverage,
    truncate,
    url_hash,
)

DOCUMENT_ID = "11111111-2222-4333-8444-555555555555"


class CanonicalUrlTest(unittest.TestCase):
    """Таблица случаев канонизации URL."""

    CASES = (
        ("https://WWW.Example.COM/Path/", "https://example.com/Path"),
        ("https://example.com/path?utm_source=rss&id=7", "https://example.com/path?id=7"),
        ("https://example.com/path?fbclid=abc", "https://example.com/path"),
        ("https://example.com/path?gclid=1&ref=x&q=2", "https://example.com/path?q=2"),
        ("https://example.com/path#section", "https://example.com/path"),
        ("https://example.com/", "https://example.com"),
        ("HTTP://Example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a?b=", "https://example.com/a?b="),
    )

    def test_canonical_forms(self) -> None:
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(canonical_url(raw), expected)

    def test_relative_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_url("/relative/path")

    def test_origin_domain_strips_www(self) -> None:
        self.assertEqual(origin_domain("https://www.cnews.ru/news/1"), "cnews.ru")

    def test_url_hash_is_stable_and_differs(self) -> None:
        first = url_hash(canonical_url("https://example.com/a?utm_source=x"))
        second = url_hash(canonical_url("https://www.example.com/a/"))
        self.assertEqual(first, second)
        self.assertNotEqual(first, url_hash(canonical_url("https://example.com/b")))


class HashAndTextTest(unittest.TestCase):
    """Хеш содержимого и нормализация текста."""

    def test_normalize_text_removes_punctuation_and_case(self) -> None:
        self.assertEqual(normalize_text("  Привет,   МИР!!! "), "привет мир")

    def test_content_hash_ignores_punctuation_and_case(self) -> None:
        self.assertEqual(
            content_hash("Квантовые сенсоры", "Раннее применение."),
            content_hash("квантовые  сенсоры!", "раннее применение"),
        )

    def test_content_hash_uses_only_first_2000_characters(self) -> None:
        base = "a" * 2000
        self.assertEqual(content_hash("t", base), content_hash("t", base + " хвост"))

    def test_truncate_cuts_on_word_boundary(self) -> None:
        self.assertEqual(truncate("слово другое слово", 12), "слово другое")

    def test_control_characters_become_separators(self) -> None:
        # Cc (NUL, табуляция) разделяют слова: иначе соседние слова склеиваются в одно.
        self.assertEqual(clean_whitespace("текст\x00\tс мусором"), "текст с мусором")

    def test_zero_width_characters_are_removed(self) -> None:
        # Cf (zero-width space) — невидимый символ переноса, не разделитель слов.
        self.assertEqual(clean_whitespace("сло\u200bво"), "слово")


class LanguageTest(unittest.TestCase):
    """Определение и нормализация языка."""

    def test_russian_text(self) -> None:
        self.assertEqual(detect_language("Нейроморфные чипы применяются в периферийных устройствах"), "ru")

    def test_english_text(self) -> None:
        self.assertEqual(detect_language("Neuromorphic chips are used in edge devices"), "en")

    def test_unknown_for_empty(self) -> None:
        self.assertEqual(detect_language("  "), "und")

    def test_normalize_language_code(self) -> None:
        self.assertEqual(normalize_language_code("EN-GB"), "en")
        self.assertEqual(normalize_language_code("xx-yy-zz"), "xx")
        self.assertEqual(normalize_language_code("не язык"), "")
        self.assertEqual(normalize_language_code(None), "")


class DoiAndTitleTest(unittest.TestCase):
    """DOI и ключ кеша энциклопедии."""

    def test_doi_prefix_removed(self) -> None:
        self.assertEqual(normalize_doi("https://doi.org/10.1145/ABC"), "10.1145/abc")

    def test_invalid_doi_is_none(self) -> None:
        self.assertIsNone(normalize_doi("не-doi"))
        self.assertIsNone(normalize_doi(None))

    def test_encyclopedia_title_normalized(self) -> None:
        self.assertEqual(normalize_encyclopedia_title("  Kubernetes  Engine "), "kubernetes engine")


class RelevanceTest(unittest.TestCase):
    """Доля токенов фразы в тексте (порог RSS)."""

    def test_full_coverage(self) -> None:
        self.assertEqual(term_coverage("нейроморфные чипы", "Нейроморфные чипы в проде"), 1.0)

    def test_partial_coverage(self) -> None:
        self.assertAlmostEqual(term_coverage("нейроморфные чипы", "Обычные чипы"), 0.5)

    def test_zero_coverage(self) -> None:
        self.assertEqual(term_coverage("квантовые сенсоры", "Урожай зерновых"), 0.0)


class RawMetaTest(unittest.TestCase):
    """Белый список ключей и ограничение размера `raw_meta`."""

    def test_only_allowed_keys_kept(self) -> None:
        result = sanitize_raw_meta({"type": "article", "secret": "x"}, frozenset({"type"}))
        self.assertEqual(result, {"type": "article"})

    def test_lists_are_truncated(self) -> None:
        result = sanitize_raw_meta({"topics": [f"t{i}" for i in range(50)]}, frozenset({"topics"}))
        self.assertEqual(len(result["topics"]), 10)

    def test_size_limit_enforced(self) -> None:
        payload = {"a": "x" * 6000, "b": "y" * 6000}
        result = sanitize_raw_meta(payload, frozenset({"a", "b"}))
        self.assertLessEqual(len(str(result).encode("utf-8")), MAX_RAW_META_BYTES)


class PageTokenTest(unittest.TestCase):
    """Непрозрачный keyset-токен."""

    def test_round_trip(self) -> None:
        token = encode_page_token(42, DOCUMENT_ID)
        self.assertEqual(decode_page_token(token), (42, DOCUMENT_ID))

    def test_broken_token_rejected(self) -> None:
        for token in ("!!!", "YWJj", encode_page_token(1, "короткий-id")):
            with self.subTest(token=token), self.assertRaises(ValueError):
                decode_page_token(token)


if __name__ == "__main__":
    unittest.main()
