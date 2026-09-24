"""Потолок доверенности источника: Zenodo — MEDIUM, остальные источники без изменений.

Прогон 23.09, тема «робототехника»: псевдонаучный препринт Zenodo подтверждал карточку как источник
высокой доверенности наравне с arXiv, потому что доверенность назначалась только по типу PREPRINT.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from collector.adapters.outbound.rules_loader import build_classification_config, load_classification_config
from collector.domain.classify import classify
from collector.domain.values import SourceKey, SourceType, TrustLevel

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
REAL = load_classification_config(CONFIG_DIR / "trust_rules.yaml", CONFIG_DIR / "rss_domains.yaml")


def trust(config, key: SourceKey, domain: str, dated: bool = True) -> TrustLevel:  # noqa: ANN001
    return classify(config, key, domain, "Paper", has_published_at=dated)[1]


class RealRulesTest(unittest.TestCase):
    """Проверки на действующем файле trust_rules.yaml."""

    def test_zenodo_preprint_is_medium(self) -> None:
        self.assertIs(trust(REAL, SourceKey.ZENODO, "zenodo.org"), TrustLevel.MEDIUM)

    def test_arxiv_preprint_stays_high(self) -> None:
        self.assertIs(trust(REAL, SourceKey.ARXIV, "arxiv.org"), TrustLevel.HIGH)

    def test_scientific_publications_stay_high(self) -> None:
        self.assertIs(trust(REAL, SourceKey.OPENALEX, "doi.org"), TrustLevel.HIGH)

    def test_only_zenodo_has_a_ceiling(self) -> None:
        self.assertEqual(REAL.source_trust_ceiling, {SourceKey.ZENODO: TrustLevel.MEDIUM})

    def test_undated_ceiling_still_applies(self) -> None:
        self.assertIs(trust(REAL, SourceKey.ARXIV, "arxiv.org", dated=False), TrustLevel.MEDIUM)
        self.assertIs(trust(REAL, SourceKey.ZENODO, "zenodo.org", dated=False), TrustLevel.MEDIUM)


class LoaderTest(unittest.TestCase):
    """Поле trust_ceiling читается из YAML и без него поведение прежнее."""

    RULES = {"source_defaults": {"zenodo": {"type": "PREPRINT", "trust": "HIGH"}}, "type_trust": {"PREPRINT": "HIGH"}}

    def test_without_ceiling_type_trust_applies(self) -> None:
        config = build_classification_config(self.RULES, {})
        self.assertIs(trust(config, SourceKey.ZENODO, "zenodo.org"), TrustLevel.HIGH)

    def test_ceiling_parsed_and_applied(self) -> None:
        rules = {**self.RULES, "source_defaults": {"zenodo": {"type": "PREPRINT", "trust": "MEDIUM",
                                                               "trust_ceiling": "MEDIUM"}}}
        config = build_classification_config(rules, {})
        self.assertIs(trust(config, SourceKey.ZENODO, "zenodo.org"), TrustLevel.MEDIUM)
        self.assertIs(classify(config, SourceKey.ZENODO, "zenodo.org", "Paper", has_published_at=True)[0],
                      SourceType.PREPRINT, "тип документа не меняется, меняется только доверенность")


if __name__ == "__main__":
    unittest.main()
