"""Детерминированные правила исключения и решение по скорингу (§12.6, §12.5 ТЗ).

Восемь правил применяются строго в фиксированном порядке до модели и переопределяют её решение
с явной причиной и русским объяснением. Пороги приходят из `rules.json` активной модели;
значения по умолчанию — стартовые v1 из ТЗ.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from analyzer.domain.entities import Candidate, DocumentRef, FeatureContribution
from analyzer.domain.features import pageviews_from_log
from analyzer.domain.values import Decision, DecisionReason, FeatureDirection, TrustLevel

CONTRIBUTION_NEUTRAL_BAND = 0.05


@dataclass(frozen=True, slots=True)
class RuleThresholds:
    """Пороги правил исключения (значения v1; перекрываются `rules.json` активной модели)."""

    min_query_similarity: float = 0.30
    wiki_pageviews_min: float = 20000.0
    wiki_age_min_years: float = 3.0
    mature_stage_ordinal: int = 5
    bigtech_min: float = 3.0
    share_code_vacancy_min: float = 0.4
    maturity_min: float = 0.6
    emergence_max: float = 0.2
    share_marketing_min: float = 0.6
    hype_min: float = 0.5
    trusted_share_max: float = 0.2
    # Требовать хотя бы один источник высокой доверенности (включается WS_REQUIRE_HIGH_TRUST_SOURCE).
    require_high_trust: bool = False
    model_mature_maturity_min: float = 0.4
    model_hype_min: float = 0.3

    @staticmethod
    def from_mapping(payload: dict[str, Any] | None) -> RuleThresholds:
        """Строит пороги из `rules.json`; неизвестные ключи игнорируются."""
        if not payload:
            return RuleThresholds()
        known = {field: payload[field] for field in RuleThresholds.__dataclass_fields__ if field in payload}
        return RuleThresholds(**known)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """Сработавшее правило: решение, причина и объяснение на русском."""

    decision: Decision
    reason: DecisionReason
    explanation_ru: str


def apply_exclusion_rules(
    features: dict[str, float],
    documents: Sequence[DocumentRef],
    thresholds: RuleThresholds,
    min_evidence_documents: int = 2,
) -> RuleResult | None:
    """Первое сработавшее правило из восьми либо None, если решение принимает модель.

    `min_evidence_documents` обобщает правило 3: при значении по умолчанию 2 оно срабатывает ровно
    на единственном источнике, как в §12.6 ТЗ, но параметр анализа может потребовать больше.
    """
    for rule in (
        _rule_off_topic,
        _rule_no_trusted_source,
        _rule_encyclopedia_mature,
        _rule_market_leaders,
        _rule_maturity_lexicon,
        _rule_marketing_dominant,
        _rule_hype_lexicon,
    ):
        if rule is _rule_no_trusted_source:
            result = rule(features, documents, thresholds)
            if result is not None:
                return result
            result = _rule_single_source(features, documents, thresholds, min_evidence_documents)
            if result is not None:
                return result
            result = _rule_no_high_trust_source(features, documents, thresholds)
            if result is not None:
                return result
            continue
        result = rule(features, documents, thresholds)
        if result is not None:
            return result
    return None


def _rule_off_topic(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """1. Нерелевантность запросу."""
    similarity = features.get("emb_sim_query", 0.0)
    if similarity >= thresholds.min_query_similarity:
        return None
    return RuleResult(
        Decision.OFF_TOPIC,
        DecisionReason.LOW_QUERY_RELEVANCE,
        f"Кластер слабо связан с запросом: близость {_number(similarity)} "
        f"ниже порога {_number(thresholds.min_query_similarity)}.",
    )


def _rule_no_trusted_source(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """2. Нет ни одного источника HIGH или MEDIUM."""
    if not documents or any(document.trust_level is not TrustLevel.LOW for document in documents):
        return None
    return RuleResult(
        Decision.INSUFFICIENT_EVIDENCE,
        DecisionReason.NO_TRUSTED_SOURCE,
        f"Все {len(documents)} источников имеют низкую доверенность (пресс-релизы, блоги, "
        "агрегаторы); подтверждения независимым источником нет.",
    )


def _rule_no_high_trust_source(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """2б. Нет ни одного источника высокой доверенности (если порогами требуется)."""
    if not thresholds.require_high_trust or not documents:
        return None
    if any(document.trust_level is TrustLevel.HIGH for document in documents):
        return None
    return RuleResult(
        Decision.INSUFFICIENT_EVIDENCE,
        DecisionReason.NO_TRUSTED_SOURCE,
        f"Нет ни одного источника высокой доверенности (научная публикация, препринт, патент, "
        f"официальный источник): все {len(documents)} источников средней или низкой доверенности — "
        "репозитории кода, отраслевые СМИ, вакансии. Требуется подтверждение доверенным источником.",
    )


def _rule_single_source(
    features: dict[str, float],
    documents: Sequence[DocumentRef],
    thresholds: RuleThresholds,
    min_evidence_documents: int = 2,
) -> RuleResult | None:
    """3. Источников меньше требуемого минимума, и среди них нет высокодоверенного."""
    if not documents:
        return None  # коллекции нет вовсе (ScoreText без обогащения) — правило неприменимо
    if len(documents) >= max(1, min_evidence_documents):
        return None
    if any(document.trust_level is TrustLevel.HIGH for document in documents):
        return None
    found = len(documents)
    return RuleResult(
        Decision.INSUFFICIENT_EVIDENCE,
        DecisionReason.SINGLE_SOURCE,
        f"Найдено источников: {found}, среди них нет высокодоверенного; "
        f"для слабого сигнала требуется не менее {max(1, min_evidence_documents)} "
        "независимых подтверждений.",
    )


def _rule_encyclopedia_mature(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """4. Энциклопедическая зрелость: давняя и посещаемая статья Wikipedia."""
    pageviews = pageviews_from_log(features.get("wiki_pageviews_30d_log", 0.0))
    age_years = features.get("wiki_age_years", 0.0)
    if (
        features.get("wiki_exists", 0.0) < 1.0
        or pageviews < thresholds.wiki_pageviews_min
        or age_years < thresholds.wiki_age_min_years
    ):
        return None
    return RuleResult(
        Decision.MATURE,
        DecisionReason.ENCYCLOPEDIA_MATURE,
        f"Статья в Wikipedia существует {_number(age_years)} года(лет) и набрала "
        f"{int(round(pageviews))} просмотров за 30 дней — признак сформированной технологии.",
    )


def _rule_market_leaders(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """5. Лексика массового внедрения вместе с лидерами рынка или практическим внедрением."""
    bigtech = features.get("bigtech_mentions_count", 0.0)
    code_vacancy = features.get("share_code_vacancy", 0.0)
    if features.get("stage_lex_ordinal", 0.0) < thresholds.mature_stage_ordinal:
        return None
    if bigtech < thresholds.bigtech_min and code_vacancy < thresholds.share_code_vacancy_min:
        return None
    return RuleResult(
        Decision.MATURE,
        DecisionReason.MARKET_LEADERS,
        "Тексты описывают стадию массового внедрения: упоминаний крупнейших вендоров "
        f"{int(bigtech)}, доля репозиториев и вакансий {_number(code_vacancy)} — рынок сформирован.",
    )


def _rule_maturity_lexicon(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """6. Лексика зрелости преобладает над лексикой ранней стадии."""
    maturity = features.get("lex_maturity_score", 0.0)
    emergence = features.get("lex_emergence_score", 0.0)
    if maturity < thresholds.maturity_min or emergence > thresholds.emergence_max:
        return None
    return RuleResult(
        Decision.MATURE,
        DecisionReason.MATURITY_LEXICON,
        f"Преобладает лексика сформированного рынка и стандартов ({_number(maturity)} ≥ "
        f"{_number(thresholds.maturity_min)}) при слабой лексике ранней стадии "
        f"({_number(emergence)} ≤ {_number(thresholds.emergence_max)}).",
    )


def _rule_marketing_dominant(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """7. Доминирование пресс-релизов и блогов при отсутствии научных источников."""
    marketing = features.get("share_marketing", 0.0)
    scientific = features.get("share_scientific", 0.0)
    if marketing < thresholds.share_marketing_min or scientific > 0.0:
        return None
    return RuleResult(
        Decision.HYPE_OR_NOISE,
        DecisionReason.MARKETING_DOMINANT,
        f"Доля пресс-релизов и корпоративных блогов {_number(marketing)} при полном отсутствии "
        "научных публикаций и препринтов.",
    )


def _rule_hype_lexicon(
    features: dict[str, float], documents: Sequence[DocumentRef], thresholds: RuleThresholds
) -> RuleResult | None:
    """8. Маркетинговая лексика без доверенных подтверждений."""
    hype = features.get("lex_hype_score", 0.0)
    trusted = features.get("trusted_share", 0.0)
    if hype < thresholds.hype_min or trusted > thresholds.trusted_share_max:
        return None
    return RuleResult(
        Decision.HYPE_OR_NOISE,
        DecisionReason.HYPE_LEXICON,
        f"Преобладает маркетинговая лексика ({_number(hype)} ≥ {_number(thresholds.hype_min)}) "
        f"без доверенных подтверждений: доля источников высокой доверенности {_number(trusted)}.",
    )


def decide_by_score(
    score: float,
    threshold: float,
    features: dict[str, float],
    thresholds: RuleThresholds,
) -> Decision:
    """Решение по калиброванной вероятности (§12.5, шаг 9)."""
    if score >= threshold:
        return Decision.WEAK_SIGNAL
    if (
        features.get("lex_maturity_score", 0.0) >= thresholds.model_mature_maturity_min
        or features.get("wiki_exists", 0.0) >= 1.0
    ):
        return Decision.MATURE
    if features.get("lex_hype_score", 0.0) >= thresholds.model_hype_min:
        return Decision.HYPE_OR_NOISE
    return Decision.INSUFFICIENT_EVIDENCE


def explain_model_decision(
    score: float, threshold: float, contributions: Sequence[FeatureContribution]
) -> str:
    """Объяснение решения модели по двум наибольшим по модулю вкладам признаков."""
    comparison = "не ниже порога" if score >= threshold else "ниже порога"
    top = sorted(contributions, key=lambda item: abs(item.contribution), reverse=True)[:2]
    if not top:
        return f"Калиброванная вероятность {_number(score)} {comparison} {_number(threshold)}."
    parts = ", ".join(
        f"{item.label_ru.lower()} ({_signed(item.contribution)})"
        for item in top
        if abs(item.contribution) > 0
    )
    text = (
        f"Калиброванная вероятность слабого сигнала {_number(score)} {comparison} "
        f"{_number(threshold)}."
    )
    if parts:
        text += f" Наибольший вклад: {parts}."
    return text[:500]


def direction_of(contribution: float) -> FeatureDirection:
    """Направление вклада: полоса нейтральности ±0.05 логита (§12.8 ТЗ)."""
    if contribution > CONTRIBUTION_NEUTRAL_BAND:
        return FeatureDirection.SUPPORTS_WEAK_SIGNAL
    if contribution < -CONTRIBUTION_NEUTRAL_BAND:
        return FeatureDirection.SUPPORTS_MATURE
    return FeatureDirection.NEUTRAL


def rank_weak_signals(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Ранжирование слабых сигналов: score ↓, trusted_share ↓, query_relevance ↓; ранги 1..N."""
    weak = [candidate for candidate in candidates if candidate.decision is Decision.WEAK_SIGNAL]
    ordered = sorted(
        weak,
        key=lambda candidate: (
            -candidate.score,
            -candidate.feature_value("trusted_share"),
            -candidate.query_relevance,
            candidate.cluster_index,
        ),
    )
    for position, candidate in enumerate(ordered, start=1):
        candidate.with_rank(position)
    return ordered


def _number(value: float) -> str:
    """Форматирование числа для объяснений: два знака после запятой (большие числа — целые)."""
    return f"{value:.2f}" if abs(value) < 1000 else f"{value:.0f}"


def _signed(value: float) -> str:
    """Вклад со знаком."""
    return f"{value:+.2f}"
