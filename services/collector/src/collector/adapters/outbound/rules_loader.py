"""Загрузка правил классификации из YAML в доменную конфигурацию.

Домен остаётся без файлового ввода-вывода: разбор файлов выполняется здесь, на границе адаптеров.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from collector.domain.classify import ClassificationConfig
from collector.domain.values import SourceKey, SourceType, TrustLevel


def load_classification_config(trust_rules_path: Path, rss_domains_path: Path) -> ClassificationConfig:
    """Читает `trust_rules.yaml` и `rss_domains.yaml`; ValueError при неизвестных значениях."""
    trust_rules = _read_mapping(trust_rules_path)
    rss_domains = _read_mapping(rss_domains_path)
    return build_classification_config(trust_rules, rss_domains)


def build_classification_config(
    trust_rules: dict[str, Any], rss_domains: dict[str, Any]
) -> ClassificationConfig:
    """Собирает конфигурацию классификации из разобранных структур YAML."""
    source_defaults: dict[SourceKey, tuple[SourceType, TrustLevel]] = {}
    source_trust_ceiling: dict[SourceKey, TrustLevel] = {}
    authoritative_sources: set[SourceKey] = set()
    for key, value in (trust_rules.get("source_defaults") or {}).items():
        source_defaults[SourceKey(key)] = (SourceType(value["type"]), TrustLevel(value["trust"]))
        if "trust_ceiling" in value:
            source_trust_ceiling[SourceKey(key)] = TrustLevel(value["trust_ceiling"])
        if value.get("authoritative") is True:
            authoritative_sources.add(SourceKey(key))
    type_trust = {
        SourceType(name): TrustLevel(level) for name, level in (trust_rules.get("type_trust") or {}).items()
    }
    domain_categories = {
        str(domain): SourceType(category) for domain, category in (rss_domains.get("domains") or {}).items()
    }
    return ClassificationConfig(
        source_defaults=source_defaults,
        type_trust=type_trust,
        source_trust_ceiling=source_trust_ceiling,
        authoritative_sources=frozenset(authoritative_sources),
        government_domains=tuple(trust_rules.get("government_domains") or ()),
        government_suffixes=tuple(trust_rules.get("government_suffixes") or ()),
        press_release_domains=tuple(trust_rules.get("press_release_domains") or ()),
        press_release_title_markers=tuple(
            str(marker).lower() for marker in trust_rules.get("press_release_title_markers") or ()
        ),
        domain_categories=domain_categories,
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    """Читает YAML-файл и проверяет, что на верхнем уровне находится отображение."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидается отображение на верхнем уровне")
    return data
