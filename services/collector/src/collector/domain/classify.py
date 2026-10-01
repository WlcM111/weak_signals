"""Классификация источника: тип документа и уровень доверенности (§6.5 ТЗ, §6 HANDOFF).

Правила задаются данными (`config/trust_rules.yaml`, `config/rss_domains.yaml`) и применяются чисто:
порядок — государственные домены → пресс-релизы → категория домена ленты → умолчание адаптера.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from collector.domain.values import SourceKey, SourceType, TrustLevel

NO_DATE_TRUST_CEILING = TrustLevel.MEDIUM


@dataclass(frozen=True, slots=True)
class ClassificationConfig:
    """Справочники правил доверенности; загружаются из YAML при старте сервиса."""

    source_defaults: dict[SourceKey, tuple[SourceType, TrustLevel]]
    type_trust: dict[SourceType, TrustLevel]
    government_domains: tuple[str, ...] = ()
    government_suffixes: tuple[str, ...] = ()
    press_release_domains: tuple[str, ...] = ()
    press_release_title_markers: tuple[str, ...] = ()
    domain_categories: dict[str, SourceType] = field(default_factory=dict)
    # Потолок доверенности документов источника независимо от их типа: препринт с платформы без
    # модерации (Zenodo) не должен получать доверенность препринта arXiv. Задаётся явно и только там,
    # где нужен, чтобы не понижать документы, переклассифицированные доменными правилами.
    source_trust_ceiling: dict[SourceKey, TrustLevel] = field(default_factory=dict)
    # Источники, чей тип документа не пересматривается доменными правилами: патентная платформа
    # Роспатента живёт на домене .gov.ru, но её документы — патенты, а не документы госоргана.
    authoritative_sources: frozenset[SourceKey] = frozenset()
    # Маркеры рекламных материалов (в форме fold_marker_text): такой документ в коллекцию не попадает.
    advertising_markers: tuple[str, ...] = ()

    def trust_for(self, source_type: SourceType) -> TrustLevel:
        """Уровень доверенности по типу источника (умолчание LOW для типов вне справочника)."""
        return self.type_trust.get(source_type, TrustLevel.LOW)


def domain_matches(domain: str, pattern: str) -> bool:
    """Совпадение домена с шаблоном справочника: точное или как поддомен (`feeds.example.com` ⊃ `example.com`)."""
    domain = domain.lower().removeprefix("www.")
    pattern = pattern.lower().lstrip(".").removeprefix("www.")
    return domain == pattern or domain.endswith(f".{pattern}")


def classify(
    config: ClassificationConfig,
    source_key: SourceKey,
    origin_domain: str,
    title: str,
    *,
    has_published_at: bool,
) -> tuple[SourceType, TrustLevel]:
    """Возвращает тип источника и уровень доверенности документа.

    Документ без даты публикации получает доверенность не выше MEDIUM (§6.5 ТЗ).
    """
    source_type = _resolve_source_type(config, source_key, origin_domain, title)
    trust = config.trust_for(source_type)
    ceiling = config.source_trust_ceiling.get(source_key)
    if ceiling is not None:
        trust = trust.capped_at(ceiling)
    if not has_published_at:
        trust = trust.capped_at(NO_DATE_TRUST_CEILING)
    return source_type, trust


def _resolve_source_type(
    config: ClassificationConfig, source_key: SourceKey, origin_domain: str, title: str
) -> SourceType:
    """Определяет тип источника по домену и заголовку; для не-RSS адаптеров умолчание фиксировано."""
    authoritative = config.source_defaults.get(source_key)
    if authoritative is not None and source_key in config.authoritative_sources:
        return authoritative[0]
    if any(domain_matches(origin_domain, pattern) for pattern in config.government_domains) or any(
        origin_domain.lower().endswith(suffix) for suffix in config.government_suffixes
    ):
        return SourceType.GOVERNMENT
    if _is_press_release(config, origin_domain, title):
        return SourceType.PRESS_RELEASE
    default_type = config.source_defaults.get(source_key)
    if source_key is not SourceKey.RSS and default_type is not None:
        return default_type[0]
    for pattern, category in config.domain_categories.items():
        if domain_matches(origin_domain, pattern):
            return category
    return SourceType.OTHER


def _is_press_release(config: ClassificationConfig, origin_domain: str, title: str) -> bool:
    """Пресс-релиз: домен агрегатора релизов или явный маркер в заголовке."""
    if any(domain_matches(origin_domain, pattern) for pattern in config.press_release_domains):
        return True
    lowered = title.lower()
    return any(marker in lowered for marker in config.press_release_title_markers)


def fold_marker_text(text: str) -> str:
    """Текст для поиска маркеров: NFKC, без учёта регистра, «ё» = «е», одиночные пробелы."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


def advertising_marker(config: ClassificationConfig, title: str, text: str) -> str:
    """Маркер рекламного материала в заголовке или тексте документа; пустая строка — маркера нет.

    ТЗ: маркетинг не может быть основанием для сигнала, поэтому спонсорские и партнёрские публикации
    отсекаются до записи в коллекцию, а не только понижаются в доверенности.
    """
    if not config.advertising_markers:
        return ""
    haystack = fold_marker_text(f"{title} {text}")
    return next((marker for marker in config.advertising_markers if marker in haystack), "")
