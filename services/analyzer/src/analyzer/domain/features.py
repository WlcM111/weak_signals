"""Вычисление 25 интерпретируемых признаков реестра v1 (§12.7 ТЗ).

Все функции чистые и общие для обучения и инференса: trainer обязан вызывать их же, иначе признаки
на обучении и на инференсе разойдутся. Клиппинг по диапазонам выполняет `FeatureRegistry`.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from analyzer.domain.entities import DocumentRef
from analyzer.domain.values import (
    CODE_VACANCY_TYPES,
    MARKETING_TYPES,
    NEWS_TYPES,
    SCIENTIFIC_TYPES,
    TrustLevel,
)

LEXICAL_FEATURES = (
    "lex_emergence_score",
    "lex_maturity_score",
    "lex_hype_score",
    "stage_lex_ordinal",
    "funding_mentions_count",
    "bigtech_mentions_count",
    "org_mentions_count",
    "recent_year_share",
)
COLLECTION_FEATURES = (
    "doc_count_log",
    "source_type_diversity",
    "share_scientific",
    "share_news_media",
    "share_marketing",
    "share_code_vacancy",
    "trusted_share",
    "growth_ratio_12m",
    "recency_median_days",
    "first_seen_years_ago",
    "citation_median_log",
)
ENCYCLOPEDIA_FEATURES = ("wiki_exists", "wiki_pageviews_30d_log", "wiki_age_years")
EMBEDDING_FEATURES = ("emb_sim_weak_centroid", "emb_sim_mature_centroid", "emb_sim_query")

TOKENS_PER_NORMALIZATION_UNIT = 1000
MIN_DENOMINATOR_TOKENS = 200
SATURATION_HITS_PER_1000 = 20
DAYS_PER_YEAR = 365.25
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_FUNDING_RE = re.compile(
    r"(?:[$€]\s?\d[\d\s.,]*)"
    r"|(?:\b\d[\d.,]*\s?(?:млн|млрд|тыс\.?|million|billion|bn|mln)\b)"
    r"|(?:\bseries\s+[a-e]\b)"
    r"|(?:\b(?:pre-?seed|seed)\s+(?:round|раунд|funding|инвестиц\w*)\b)"
    r"|(?:\b(?:посевн\w+|венчурн\w+)\s+(?:раунд\w*|инвестиц\w+|финансирован\w+))"
    r"|(?:\bраунд\w*\s+(?:финансирования|инвестиций)\b)",
    re.IGNORECASE | re.UNICODE,
)
_ORG_SUFFIX = r"(?:Inc|LLC|Ltd|GmbH|Labs|AI|Corp|Corporation|Technologies|Systems|Group|ООО|АО|НИИ)"
_ORG_RE = re.compile(
    rf"\b(?:[A-ZА-ЯЁ][\w&.-]*(?:\s+[A-ZА-ЯЁ][\w&.-]*){{0,3}})\s+{_ORG_SUFFIX}\b"
    rf"|\b[A-ZА-ЯЁ][\w&.-]*(?:\s+[A-ZА-ЯЁ][\w&.-]*){{1,3}}\b",
    re.UNICODE,
)


@dataclass(frozen=True, slots=True)
class TermSet:
    """Набор терминов лексикона: точные токены, префиксы (`прототип*`) и многословные фразы.

    Префиксы заменяют морфологический анализ: одна запись покрывает словоформы русского языка,
    оставаясь прозрачной для методолога, который правит лексикон. Префикс допустим и внутри фразы
    («массов* внедрение»), поэтому фразы сопоставляются регулярным выражением по границам слов,
    а не поиском подстроки: иначе «рынок» находился бы внутри «рынке».
    """

    tokens: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()
    phrase_pattern: re.Pattern[str] | None = None

    @staticmethod
    def from_terms(terms: Iterable[str]) -> TermSet:
        """Разбирает термины лексикона (регистр не учитывается)."""
        tokens: set[str] = set()
        prefixes: set[str] = set()
        phrases: set[str] = set()
        for term in terms:
            raw = term.strip().lower()
            if not raw:
                continue
            words = [word for word in raw.split() if word]
            parsed = [_parse_word(word) for word in words]
            parsed = [item for item in parsed if item is not None]
            if not parsed:
                continue
            if len(parsed) == 1:
                core, is_prefix = parsed[0]
                (prefixes if is_prefix else tokens).add(core)
                continue
            phrases.add(" ".join(f"{core}*" if is_prefix else core for core, is_prefix in parsed))
        return TermSet(
            tokens=frozenset(tokens),
            prefixes=tuple(sorted(prefixes)),
            phrases=tuple(sorted(phrases)),
            phrase_pattern=_compile_phrases(sorted(phrases)),
        )

    def count_in(self, tokens: Sequence[str], normalized_text: str) -> int:
        """Число вхождений терминов набора: точные токены, префиксы и фразы."""
        prefixes = self.prefixes
        hits = sum(
            1
            for token in tokens
            if token in self.tokens or (prefixes and token.startswith(prefixes))
        )
        if self.phrase_pattern is not None:
            hits += len(self.phrase_pattern.findall(normalized_text))
        return hits


def _parse_word(word: str) -> tuple[str, bool] | None:
    """Разбирает слово термина: основа и признак «префикс словоформы»."""
    is_prefix = word.endswith("*")
    core_tokens = _TOKEN_RE.findall(word.rstrip("*"))
    if not core_tokens:
        return None
    return "".join(core_tokens), is_prefix


def _compile_phrases(phrases: Sequence[str]) -> re.Pattern[str] | None:
    """Собирает одно регулярное выражение для всех фраз лексикона."""
    if not phrases:
        return None
    alternatives: list[str] = []
    for phrase in phrases:
        parts = []
        for word in phrase.split():
            is_prefix = word.endswith("*")
            core = re.escape(word.rstrip("*"))
            parts.append(f"{core}\\w*" if is_prefix else f"{core}\\b")
        alternatives.append(r"\b" + r"\s+".join(parts))
    return re.compile("|".join(alternatives), re.UNICODE)


@dataclass(frozen=True, slots=True)
class Lexicons:
    """Лексиконы признаков (ru + en объединены; источник — `config/lexicons/*.txt`)."""

    emergence: TermSet = field(default_factory=TermSet)
    maturity: TermSet = field(default_factory=TermSet)
    hype: TermSet = field(default_factory=TermSet)
    bigtech: TermSet = field(default_factory=TermSet)
    stopwords: frozenset[str] = frozenset()
    stage_terms: dict[int, TermSet] = field(default_factory=dict)


def tokenize(text: str) -> list[str]:
    """Токены в нижнем регистре."""
    return _TOKEN_RE.findall(text.lower())


def normalized_text(text: str) -> str:
    """Текст в нижнем регистре со схлопнутыми пробелами (для поиска фраз)."""
    return " ".join(tokenize(text))


def lexicon_rate(hits: int, total_tokens: int) -> float:
    """Плотность терминов лексикона: доля токенов на 1000, приведённая к шкале 0..1 (§12.7 ТЗ).

    Две константы задают шкалу. `SATURATION_HITS_PER_1000` = 20: плотность 2 % токенов лексикона
    считается предельной и даёт 1.0, поэтому пороги правил v1 (0.6 для зрелости, 0.5 для хайпа)
    соответствуют содержательно плотной лексике, а не единственному слову. `MIN_DENOMINATOR_TOKENS`
    = 200 не даёт короткому тексту (описание из 40 слов в ScoreText) мгновенно упереться в 1.0
    из-за одного совпадения. Функция общая для trainer и analyzer; пороги калибруются на этой же
    шкале и передаются в `rules.json`.
    """
    if total_tokens <= 0 or hits <= 0:
        return 0.0
    denominator = max(total_tokens, MIN_DENOMINATOR_TOKENS)
    per_thousand = hits / denominator * TOKENS_PER_NORMALIZATION_UNIT
    return min(1.0, per_thousand / SATURATION_HITS_PER_1000)


def lexical_features(text: str, lexicons: Lexicons, now: datetime) -> dict[str, float]:
    """Восемь лексических признаков по тексту наблюдения."""
    tokens = tokenize(text)
    normalized = " ".join(tokens)
    total = len(tokens)
    return {
        "lex_emergence_score": lexicon_rate(lexicons.emergence.count_in(tokens, normalized), total),
        "lex_maturity_score": lexicon_rate(lexicons.maturity.count_in(tokens, normalized), total),
        "lex_hype_score": lexicon_rate(lexicons.hype.count_in(tokens, normalized), total),
        "stage_lex_ordinal": float(_stage_ordinal(tokens, normalized, lexicons)),
        "funding_mentions_count": float(len(_FUNDING_RE.findall(text))),
        "bigtech_mentions_count": float(lexicons.bigtech.count_in(tokens, normalized)),
        "org_mentions_count": float(_org_mentions(text)),
        "recent_year_share": _recent_year_share(text, now),
    }


def _stage_ordinal(tokens: Sequence[str], normalized: str, lexicons: Lexicons) -> int:
    """Максимальная упомянутая стадия 0..5 (`config/stage_rules.yaml`)."""
    maximum = 0
    for stage, terms in lexicons.stage_terms.items():
        if terms.count_in(tokens, normalized) > 0:
            maximum = max(maximum, stage)
    return maximum


def _org_mentions(text: str) -> int:
    """Эвристика числа упомянутых организаций: различные капитализированные последовательности."""
    found = {match.group(0).strip() for match in _ORG_RE.finditer(text)}
    return len(found)


def _recent_year_share(text: str, now: datetime) -> float:
    """Доля упоминаний последних двух лет среди всех упомянутых годов 2000..now+1."""
    years = [int(value) for value in _YEAR_RE.findall(text)]
    years = [year for year in years if 2000 <= year <= now.year + 1]
    if not years:
        return 0.0
    recent = sum(1 for year in years if year >= now.year - 1)
    return recent / len(years)


def collection_features(documents: Sequence[DocumentRef], now: datetime) -> dict[str, float]:
    """Одиннадцать коллекционных признаков по документам кластера или ENRICHMENT-коллекции."""
    total = len(documents)
    if total == 0:
        return empty_collection_features()
    types = [document.source_type for document in documents]
    dated = [document for document in documents if document.published_at is not None]
    ages_days = [
        max(0.0, (now - document.published_at).total_seconds() / 86400.0)  # type: ignore[operator]
        for document in dated
    ]
    last12 = sum(1 for age in ages_days if age <= DAYS_PER_YEAR)
    prev12 = sum(1 for age in ages_days if DAYS_PER_YEAR < age <= 2 * DAYS_PER_YEAR)
    citations = [
        float(document.citation_count)
        for document in documents
        if document.source_type in SCIENTIFIC_TYPES and document.citation_count is not None
    ]
    return {
        "doc_count_log": math.log1p(total),
        "source_type_diversity": float(len(set(types))),
        "share_scientific": _share(types, SCIENTIFIC_TYPES),
        "share_news_media": _share(types, NEWS_TYPES),
        "share_marketing": _share(types, MARKETING_TYPES),
        "share_code_vacancy": _share(types, CODE_VACANCY_TYPES),
        "trusted_share": sum(1 for document in documents if document.trust_level is TrustLevel.HIGH) / total,
        "growth_ratio_12m": (last12 + 1) / (prev12 + 1),
        "recency_median_days": statistics.median(ages_days) if ages_days else 0.0,
        "first_seen_years_ago": (max(ages_days) / DAYS_PER_YEAR) if ages_days else 0.0,
        "citation_median_log": math.log1p(statistics.median(citations)) if citations else 0.0,
    }


def _share(types: Sequence[object], group: frozenset[object]) -> float:
    """Доля документов указанных типов."""
    return sum(1 for source_type in types if source_type in group) / len(types)


def empty_collection_features() -> dict[str, float]:
    """Значения коллекционных признаков «пустой коллекции» (§12.7 ТЗ): доли 0, объём 0."""
    return dict.fromkeys(COLLECTION_FEATURES, 0.0) | {"growth_ratio_12m": 1.0}


@dataclass(frozen=True, slots=True)
class EncyclopediaSignal:
    """Лучший результат `CheckEncyclopedia` по названию и ключевым фразам кандидата."""

    exists: bool = False
    pageviews_30d: int = 0
    created_at: datetime | None = None


def encyclopedia_features(signal: EncyclopediaSignal, now: datetime) -> dict[str, float]:
    """Три энциклопедических признака; недоступные просмотры (-1) считаются нулём."""
    if not signal.exists:
        return {"wiki_exists": 0.0, "wiki_pageviews_30d_log": 0.0, "wiki_age_years": 0.0}
    age_years = 0.0
    if signal.created_at is not None:
        age_years = max(0.0, (now - signal.created_at).total_seconds() / 86400.0 / DAYS_PER_YEAR)
    return {
        "wiki_exists": 1.0,
        "wiki_pageviews_30d_log": math.log1p(max(signal.pageviews_30d, 0)),
        "wiki_age_years": age_years,
    }


def embedding_features(
    centroid: Sequence[float],
    weak_centroid: Sequence[float] | None,
    mature_centroid: Sequence[float] | None,
    query_vector: Sequence[float] | None,
) -> dict[str, float]:
    """Три эмбеддинговых признака: близость к эталонным центроидам и к запросу."""
    return {
        "emb_sim_weak_centroid": cosine(centroid, weak_centroid),
        "emb_sim_mature_centroid": cosine(centroid, mature_centroid),
        "emb_sim_query": cosine(centroid, query_vector),
    }


def cosine(first: Sequence[float] | None, second: Sequence[float] | None) -> float:
    """Косинусная близость двух векторов; 0.0, если вектор отсутствует или нулевой."""
    if first is None or second is None or len(first) != len(second) or not len(first):
        return 0.0
    dot = 0.0
    norm_first = 0.0
    norm_second = 0.0
    for left, right in zip(first, second, strict=True):
        dot += left * right
        norm_first += left * left
        norm_second += right * right
    if norm_first <= 0.0 or norm_second <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / math.sqrt(norm_first * norm_second)))


def pageviews_from_log(value: float) -> float:
    """Обратное преобразование `wiki_pageviews_30d_log` (для текста объяснения правила 4)."""
    return math.expm1(value)
