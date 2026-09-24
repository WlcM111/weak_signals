"""Признаки v2: тема + название кандидата + свидетельства. Один код для обучения (ml) и analyzer.

Признаки ограничены по построению и не зависят от длины текста: доли считаются по документам, а не
по склейке кластера (в v1 счётчики росли с числом документов и выходили за обучающий диапазон).
Эмбеддинговые компоненты — проекция, замороженная на Stage A и хранящаяся в артефакте.
Объяснение метки, происхождение разметки и итоговые оценки на вход не подаются.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from analyzer.domain.features import _FUNDING_RE, Lexicons, tokenize
from analyzer.domain.values import CODE_VACANCY_TYPES, MARKETING_TYPES, NEWS_TYPES, SCIENTIFIC_TYPES
from ws_common.query_fallback import fallback_terms

FEATURE_SCHEMA_V2 = "v2"
EMBEDDING_COMPONENTS = 8
MAX_EVIDENCE = 8
STEM_LENGTH = 5
MIN_TOKEN = 3
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
_SCI = frozenset(item.value for item in SCIENTIFIC_TYPES)
_NEWS = frozenset(item.value for item in NEWS_TYPES)
_MKT = frozenset(item.value for item in MARKETING_TYPES)
_CODE = frozenset(item.value for item in CODE_VACANCY_TYPES)
_OVERVIEW_RE = re.compile(
    r"\b(?:reviews?|survey|overview|state[- ]of[- ]the[- ]art|trends|prospects?|perspectives?|challenges|"
    r"opportunities|roadmap|landscape|systematic|обзор\w*|перспектив\w*|тенденц\w*|проблем\w*|"
    r"аналитическ\w*|возможност\w*)\b",
    re.IGNORECASE | re.UNICODE,
)

# (имя, группа, нижняя граница, верхняя граница, подпись). Порядок = порядок вектора модели.
SCALAR_SPECS: tuple[tuple[str, str, float, float, str], ...] = (
    ("rel_query_title", "relevance", -1.0, 1.0, "Близость названия кандидата к теме запроса"),
    ("rel_query_evidence_mean", "relevance", -1.0, 1.0, "Средняя близость свидетельств к теме"),
    ("rel_query_evidence_max", "relevance", -1.0, 1.0, "Максимальная близость свидетельства к теме"),
    ("rel_cluster_query_v1", "relevance", 0.0, 1.0, "Близость центроида кластера к запросу с расширениями (как в v1)"),
    ("rel_terms_title", "relevance", 0.0, 1.0, "Доля терминов темы в названии"),
    ("rel_terms_evidence_share", "relevance", 0.0, 1.0, "Доля свидетельств с терминами темы"),
    ("has_topic", "relevance", 0.0, 1.0, "Задана тема запроса (0 — прямой скоринг описания)"),
    ("spec_title_length", "specificity", 0.0, 1.0, "Содержательная длина названия"),
    ("spec_title_topic_overlap", "specificity", 0.0, 1.0, "Название повторяет тему (пересказ запроса)"),
    ("spec_overview_share", "specificity", 0.0, 1.0, "Доля обзоров и аналитики среди свидетельств"),
    ("spec_evidence_cohesion", "specificity", -1.0, 1.0, "Согласованность свидетельств между собой"),
    ("spec_title_evidence_sim", "specificity", -1.0, 1.0, "Близость названия к свидетельствам"),
    ("lex_emergence_share", "lexical", 0.0, 1.0, "Доля текстов с лексикой ранней стадии"),
    ("lex_maturity_share", "lexical", 0.0, 1.0, "Доля текстов с лексикой зрелости"),
    ("lex_hype_share", "lexical", 0.0, 1.0, "Доля текстов с маркетинговой лексикой"),
    ("lex_stage_max", "lexical", 0.0, 1.0, "Максимальная упомянутая стадия (0..1)"),
    ("lex_funding_share", "lexical", 0.0, 1.0, "Доля текстов с упоминанием финансирования"),
    ("lex_bigtech_share", "lexical", 0.0, 1.0, "Доля текстов с упоминанием крупнейших вендоров"),
    ("has_evidence", "metadata", 0.0, 1.0, "Есть свидетельства"),
    ("evidence_count", "metadata", 0.0, 1.0, "Число свидетельств (до 8, нормировано)"),
    ("share_scientific", "metadata", 0.0, 1.0, "Доля научных публикаций и препринтов"),
    ("share_news_media", "metadata", 0.0, 1.0, "Доля новостей и отраслевых СМИ"),
    ("share_marketing", "metadata", 0.0, 1.0, "Доля пресс-релизов и корпоративных блогов"),
    ("share_code", "metadata", 0.0, 1.0, "Доля репозиториев и вакансий"),
    ("trusted_share", "metadata", 0.0, 1.0, "Доля источников высокой доверенности"),
    ("low_trust_share", "metadata", 0.0, 1.0, "Доля источников низкой доверенности"),
    ("source_diversity", "metadata", 0.0, 1.0, "Разнообразие типов источников"),
    ("recent_publication_share", "metadata", 0.0, 1.0, "Доля свидетельств за последние два года"),
    ("dated_share", "metadata", 0.0, 1.0, "Доля свидетельств с датой"),
)
# Признаки, определённые только при заданной теме и свидетельствах: при прямом скоринге описания они равны 0.
TOPIC_DEPENDENT = frozenset(
    name for name, group, *_ in SCALAR_SPECS if group in ("relevance", "metadata") and name != "has_topic"
) | {"spec_title_topic_overlap", "spec_evidence_cohesion", "spec_title_evidence_sim"}
EMBEDDING_NAMES = tuple(f"emb_pc_{index}" for index in range(1, EMBEDDING_COMPONENTS + 1))
FEATURE_NAMES_V2 = tuple(spec[0] for spec in SCALAR_SPECS) + EMBEDDING_NAMES
FEATURE_GROUPS_V2 = {spec[0]: spec[1] for spec in SCALAR_SPECS} | dict.fromkeys(EMBEDDING_NAMES, "embedding")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Свидетельство так, как его видит analyzer: название и метаданные документа."""

    title: str
    source_type: str
    trust_level: str
    published_year: int | None = None


@dataclass(frozen=True, slots=True)
class Projection:
    """Замороженная на Stage A проекция эмбеддинга: центр, компоненты, масштаб."""

    mean: np.ndarray
    components: np.ndarray
    scale: np.ndarray

    def project(self, vector: np.ndarray) -> np.ndarray:
        """Координаты вектора в пространстве компонент, клиппинг ±5."""
        values = (np.asarray(vector, dtype=np.float64) - self.mean) @ self.components.T / self.scale
        return np.clip(values, -5.0, 5.0)

    def to_json(self) -> dict[str, list]:
        """Сериализация для артефакта."""
        return {"mean": self.mean.tolist(), "components": self.components.tolist(), "scale": self.scale.tolist()}

    @staticmethod
    def from_json(payload: Mapping[str, list]) -> Projection:
        """Восстановление из артефакта."""
        return Projection(
            mean=np.asarray(payload["mean"], dtype=np.float64),
            components=np.asarray(payload["components"], dtype=np.float64),
            scale=np.asarray(payload["scale"], dtype=np.float64),
        )


@dataclass(frozen=True, slots=True)
class ObservationVectors:
    """Эмбеддинги темы (или None), названия и названий свидетельств."""

    topic: np.ndarray | None
    title: np.ndarray
    evidence: np.ndarray


def registry_payload() -> dict:
    """JSON реестра признаков v2 (schemas/feature_registry_v2.json и копия в артефакте)."""
    features = [
        {"name": name, "group": group, "type": "float", "range": [low, high], "label_ru": label,
         "expected_direction": "neutral", "used_in_model": True}
        for name, group, low, high, label in SCALAR_SPECS
    ]
    features += [
        {"name": name, "group": "embedding", "type": "float", "range": [-5.0, 5.0],
         "label_ru": f"Компонента {name[-1]} смыслового профиля (проекция Stage A)",
         "expected_direction": "neutral", "used_in_model": True}
        for name in EMBEDDING_NAMES
    ]
    return {
        "feature_schema_version": FEATURE_SCHEMA_V2,
        "description": "Признаки v2: тема + кандидат + свидетельства; ограничены по построению, общий код "
        "ml и analyzer (analyzer.domain.features_v2). Направления признаков выучиваются моделью.",
        "features": features,
    }


def _unit(vector: np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(array))
    return array / norm if norm > 0 else array


def _stems(text: str, stopwords: frozenset[str]) -> set[str]:
    return {token[:STEM_LENGTH] for token in tokenize(text) if len(token) >= MIN_TOKEN and token not in stopwords}


def topic_terms(topic: str, glossary: Mapping[str, str], stopwords: frozenset[str]) -> set[str]:
    """Основы содержательных слов темы и их перевода по глоссарию артефакта."""
    if not topic.strip():
        return set()
    ru_terms, en_terms = fallback_terms(topic, dict(glossary))
    stems: set[str] = set()
    for phrase in (*ru_terms, *en_terms):
        stems |= _stems(phrase, stopwords)
    return stems


def observation_features(
    topic: str,
    title: str,
    evidence: Sequence[EvidenceItem],
    vectors: ObservationVectors,
    projection: Projection,
    lexicons: Lexicons,
    as_of_year: int,
    glossary: Mapping[str, str],
    cluster_query_similarity: float | None = None,
) -> dict[str, float]:
    """Вектор признаков v2 одного наблюдения; без темы признаки из TOPIC_DEPENDENT равны 0."""
    evidence = list(evidence)[:MAX_EVIDENCE]
    count = len(evidence)
    stopwords = frozenset(lexicons.stopwords)
    title_vec = _unit(vectors.title)
    ev_vecs = np.asarray(vectors.evidence, dtype=np.float64).reshape(count, -1) if count else np.zeros((0, 1))
    if count:
        ev_vecs = ev_vecs / np.maximum(np.linalg.norm(ev_vecs, axis=1, keepdims=True), 1e-12)
    values: dict[str, float] = dict.fromkeys(FEATURE_NAMES_V2, 0.0)
    has_topic = bool(topic.strip()) and vectors.topic is not None
    title_stems = _stems(title, stopwords)
    if has_topic:
        query = _unit(vectors.topic)  # type: ignore[arg-type]
        terms = topic_terms(topic, glossary, stopwords)
        values["has_topic"] = 1.0
        values["rel_query_title"] = float(query @ title_vec)
        if cluster_query_similarity is not None:
            values["rel_cluster_query_v1"] = float(cluster_query_similarity)
        if count:
            sims = ev_vecs @ query
            values["rel_query_evidence_mean"] = float(sims.mean())
            values["rel_query_evidence_max"] = float(sims.max())
        if terms:
            values["rel_terms_title"] = len(title_stems & terms) / len(terms)
            if count:
                values["rel_terms_evidence_share"] = sum(
                    1 for item in evidence if _stems(item.title, stopwords) & terms
                ) / count
            if title_stems:
                values["spec_title_topic_overlap"] = len(title_stems & terms) / len(title_stems)
    values["spec_title_length"] = min(len(title_stems), 12) / 12.0
    documents = [item.title for item in evidence] or [title]
    values["spec_overview_share"] = sum(1 for text in documents if _OVERVIEW_RE.search(text)) / len(documents)
    if count >= 2 and has_topic:
        pair = ev_vecs @ ev_vecs.T
        values["spec_evidence_cohesion"] = float((pair.sum() - count) / (count * (count - 1)))
    if count and has_topic:
        values["spec_title_evidence_sim"] = float((ev_vecs @ title_vec).mean())
    texts = [title, *[item.title for item in evidence]]
    hits = {"emergence": 0, "maturity": 0, "hype": 0, "funding": 0, "bigtech": 0}
    stage_max = 0
    for text in texts:
        tokens = tokenize(text)
        normalized = " ".join(tokens)
        hits["emergence"] += int(lexicons.emergence.count_in(tokens, normalized) > 0)
        hits["maturity"] += int(lexicons.maturity.count_in(tokens, normalized) > 0)
        hits["hype"] += int(lexicons.hype.count_in(tokens, normalized) > 0)
        hits["bigtech"] += int(lexicons.bigtech.count_in(tokens, normalized) > 0)
        hits["funding"] += int(bool(_FUNDING_RE.search(text)))
        for stage, terms in lexicons.stage_terms.items():
            if terms.count_in(tokens, normalized) > 0:
                stage_max = max(stage_max, stage)
    for key in ("emergence", "maturity", "hype", "funding", "bigtech"):
        values[f"lex_{key}_share"] = hits[key] / len(texts)
    values["lex_stage_max"] = min(stage_max, 5) / 5.0
    if count and has_topic:
        types = [item.source_type for item in evidence]
        values["has_evidence"] = 1.0
        values["evidence_count"] = min(count, MAX_EVIDENCE) / MAX_EVIDENCE
        values["share_scientific"] = sum(1 for t in types if t in _SCI) / count
        values["share_news_media"] = sum(1 for t in types if t in _NEWS) / count
        values["share_marketing"] = sum(1 for t in types if t in _MKT) / count
        values["share_code"] = sum(1 for t in types if t in _CODE) / count
        values["trusted_share"] = sum(1 for item in evidence if item.trust_level == "HIGH") / count
        values["low_trust_share"] = sum(1 for item in evidence if item.trust_level == "LOW") / count
        values["source_diversity"] = min(len(set(types)), 4) / 4.0
        years = [item.published_year for item in evidence if item.published_year]
        values["dated_share"] = len(years) / count
        if years:
            values["recent_publication_share"] = sum(1 for year in years if year >= as_of_year - 1) / len(years)
    combined = title_vec + (ev_vecs.mean(axis=0) if count else 0.0)
    for name, value in zip(EMBEDDING_NAMES, projection.project(_unit(combined)), strict=True):
        values[name] = float(value)
    return {name: (0.0 if not math.isfinite(value) else float(value)) for name, value in values.items()}
