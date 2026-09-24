"""Классификация источников по `config/trust_rules.yaml` и `config/rss_domains.yaml` (§6.5 ТЗ)."""

from __future__ import annotations

import unittest
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.domain.classify import classify, domain_matches
from collector.domain.values import SourceKey, SourceType, TrustLevel

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ClassifyTest(unittest.TestCase):
    """Все ветки правил доверенности."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_classification_config(
            CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml"
        )

    def assert_classified(
        self,
        source_key: SourceKey,
        domain: str,
        title: str,
        expected_type: SourceType,
        expected_trust: TrustLevel,
        *,
        has_date: bool = True,
    ) -> None:
        result = classify(self.config, source_key, domain, title, has_published_at=has_date)
        self.assertEqual(result, (expected_type, expected_trust))

    def test_adapter_defaults(self) -> None:
        self.assert_classified(
            SourceKey.OPENALEX, "dl.acm.org", "Статья", SourceType.SCIENTIFIC_PUBLICATION, TrustLevel.HIGH
        )
        self.assert_classified(SourceKey.ARXIV, "arxiv.org", "Препринт", SourceType.PREPRINT, TrustLevel.HIGH)
        self.assert_classified(
            SourceKey.PATENTSVIEW, "patentsview.org", "Патент", SourceType.PATENT, TrustLevel.HIGH
        )
        self.assert_classified(
            SourceKey.GITHUB, "github.com", "repo/name", SourceType.CODE_REPOSITORY, TrustLevel.MEDIUM
        )
        self.assert_classified(SourceKey.HH, "hh.ru", "Вакансия", SourceType.VACANCY, TrustLevel.MEDIUM)
        self.assert_classified(
            SourceKey.WIKIPEDIA, "en.wikipedia.org", "Статья", SourceType.ENCYCLOPEDIA, TrustLevel.MEDIUM
        )

    def test_rss_domain_categories(self) -> None:
        self.assert_classified(
            SourceKey.RSS, "cnews.ru", "Новость", SourceType.INDUSTRY_MEDIA, TrustLevel.MEDIUM
        )
        self.assert_classified(SourceKey.RSS, "techcrunch.com", "Новость", SourceType.NEWS, TrustLevel.MEDIUM)
        self.assert_classified(
            SourceKey.RSS, "openai.com", "Блог", SourceType.CORPORATE_BLOG, TrustLevel.LOW
        )

    def test_subdomain_matches_category(self) -> None:
        self.assert_classified(
            SourceKey.RSS, "feeds.arstechnica.com", "Новость", SourceType.INDUSTRY_MEDIA, TrustLevel.MEDIUM
        )

    def test_press_release_by_domain_and_title(self) -> None:
        self.assert_classified(
            SourceKey.RSS, "prnewswire.com", "Anything", SourceType.PRESS_RELEASE, TrustLevel.LOW
        )
        self.assert_classified(
            SourceKey.RSS, "cnews.ru", "Пресс-релиз компании", SourceType.PRESS_RELEASE, TrustLevel.LOW
        )

    def test_government_domains_win(self) -> None:
        for domain in ("nist.gov", "digital.gov.uk", "europa.eu", "cbr.ru", "who.int"):
            with self.subTest(domain=domain):
                self.assert_classified(
                    SourceKey.RSS, domain, "Регламент", SourceType.GOVERNMENT, TrustLevel.HIGH
                )

    def test_unknown_domain_is_other_low(self) -> None:
        self.assert_classified(
            SourceKey.RSS, "unknown-blog.example", "Заметка", SourceType.OTHER, TrustLevel.LOW
        )

    def test_missing_date_caps_trust_at_medium(self) -> None:
        self.assert_classified(
            SourceKey.OPENALEX,
            "dl.acm.org",
            "Статья",
            SourceType.SCIENTIFIC_PUBLICATION,
            TrustLevel.MEDIUM,
            has_date=False,
        )

    def test_missing_date_does_not_raise_low(self) -> None:
        self.assert_classified(
            SourceKey.RSS, "openai.com", "Блог", SourceType.CORPORATE_BLOG, TrustLevel.LOW, has_date=False
        )

    def test_domain_matches_rules(self) -> None:
        self.assertTrue(domain_matches("feeds.example.com", "example.com"))
        self.assertTrue(domain_matches("example.com", "www.example.com"))
        self.assertFalse(domain_matches("notexample.com", "example.com"))


if __name__ == "__main__":
    unittest.main()
