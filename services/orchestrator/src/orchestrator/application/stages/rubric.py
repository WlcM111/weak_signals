"""Отбор слабых сигналов в режиме rubric: состав источников, предфильтр, балл «стадия + тренд», ключевые предикторы.

Обоснование (датасет B v2, 234 размеченные карточки живой выдачи, 26.09.2026):
- 94 % ошибок выдачи — шум, чужая тема, обзоры и общие понятия, а не зрелые технологии (6 %); их отличает
  содержание, поэтому решение принимает рубричная оценка LLM с полным контекстом источников;
- карточки только из GitHub релевантны в 5 случаях из 40, только из препринтов — в 0 из 8; ТЗ: соцсети, блоги,
  пресс-релизы и т. п. не могут быть единственным основанием — отсюда требование независимого доверенного источника;
- правило «больше половины источников — обзоры» убирает 31 негатив и 2 позитива;
- балл = стадия + тренд — как у организаторов: score_calc = stage + trend во всех 100 строках их датасета.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from orchestrator.application.dto import CandidateView, DocumentView, JudgeVerdictView
from orchestrator.domain.entities import FeatureRow
from orchestrator.domain.values import Decision, FeatureDirection

# Типы, которые сами по себе не подтверждают технологию (ТЗ: первичный индикатор, не единственное основание).
NON_INDEPENDENT_TYPES = frozenset(
    {"CODE_REPOSITORY", "PRESS_RELEASE", "CORPORATE_BLOG", "SOCIAL_MEDIA", "OTHER", "VACANCY", "ENCYCLOPEDIA"}
)
MARKET_TYPES = frozenset({"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT"})
REVIEW_RE = re.compile(
    r"\b(review|survey|overview|roadmap|state[- ]of[- ]the[- ]art|perspective|tutorial|progress|advances|trends|"
    r"prospects|challenges|обзор|перспектив|тенденци)\w*",
    re.IGNORECASE,
)
TYPE_RU = {
    "SCIENTIFIC_PUBLICATION": "научных публикаций", "PREPRINT": "препринтов", "PATENT": "патентов",
    "NEWS": "новостей", "INDUSTRY_MEDIA": "отраслевых СМИ", "CORPORATE_BLOG": "блогов компаний",
    "PRESS_RELEASE": "пресс-релизов", "CODE_REPOSITORY": "репозиториев кода", "VACANCY": "вакансий",
    "ANALYTICAL_REPORT": "аналитических отчётов", "GOVERNMENT": "госисточников", "SOCIAL_MEDIA": "соцсетей",
    "ENCYCLOPEDIA": "энциклопедий", "OTHER": "прочих",
}
STAGE_RU = {1: "концепция или исследование", 2: "прототип или PoC", 3: "пилот", 4: "раннее внедрение"}
TREND_RU = {1: "слабый интерес", 2: "растёт", 3: "растёт быстро"}
CODE_RU = {
    "R": "слабый сигнал", "N-OFF": "не относится к запросу", "N-GEN": "общее понятие без конкретики",
    "N-OVR": "обзор или аналитика без конкретной технологии", "N-MAT": "зрелая, массово внедрённая технология",
    "N-NOI": "шум: несвязанные документы или личные проекты", "N-HYP": "хайп без проверяемого содержания",
    "N-FUND": "новость о финансировании без технологии", "U": "данных недостаточно для решения",
}
CODE_DECISION = {
    "N-OFF": Decision.OFF_TOPIC, "N-GEN": Decision.INSUFFICIENT_EVIDENCE, "N-OVR": Decision.INSUFFICIENT_EVIDENCE,
    "N-MAT": Decision.MATURE, "N-NOI": Decision.HYPE_OR_NOISE, "N-HYP": Decision.HYPE_OR_NOISE,
    "N-FUND": Decision.HYPE_OR_NOISE, "U": Decision.INSUFFICIENT_EVIDENCE,
}


@dataclass(frozen=True, slots=True)
class Composition:
    """Состав источников кандидата и динамика упоминаний по годам."""

    total: int
    by_type: dict[str, int]
    reviews: int
    independent: int
    market: int
    years: dict[int, int]

    @property
    def review_share(self) -> float:
        return self.reviews / self.total if self.total else 0.0

    @property
    def recent_share(self) -> float:
        """Доля датированных документов двух последних лет выборки — простая мера динамики."""
        dated = sum(self.years.values())
        if not dated:
            return 0.0
        latest = max(self.years)
        return sum(count for year, count in self.years.items() if year >= latest - 1) / dated


def is_independent(document: DocumentView) -> bool:
    """Независимый доверенный источник: не репозиторий, не блог, не пресс-релиз, не самоопубликованный препринт."""
    if document.source_type in NON_INDEPENDENT_TYPES:
        return False
    trust = str(getattr(document.trust_level, "value", document.trust_level))
    return not (document.source_type == "PREPRINT" and trust != "HIGH")


def composition_of(documents: Sequence[DocumentView]) -> Composition:
    """Сводка состава источников."""
    years: Counter = Counter()
    for document in documents:
        if isinstance(document.published_at, datetime):
            years[document.published_at.year] += 1
    return Composition(
        total=len(documents),
        by_type=dict(Counter(document.source_type for document in documents)),
        reviews=sum(1 for document in documents if REVIEW_RE.search(document.title or "")),
        independent=sum(1 for document in documents if is_independent(document)),
        market=sum(1 for document in documents if document.source_type in MARKET_TYPES),
        years=dict(sorted(years.items())),
    )


def composition_ru(composition: Composition) -> str:
    """Сводка для рубричной оценки: состав, независимые и рыночные источники, динамика по годам."""
    if not composition.total:
        return "Источников нет."
    types = ", ".join(f"{TYPE_RU.get(kind, kind)} {count}" for kind, count in
                      sorted(composition.by_type.items(), key=lambda item: -item[1]))
    years = ", ".join(f"{year} — {count}" for year, count in composition.years.items()) or "даты неизвестны"
    return (f"Источников {composition.total}: {types}; обзоров {composition.reviews}. Независимых доверенных "
            f"{composition.independent}, рыночных (СМИ, аналитика, пресс-релизы) {composition.market}. "
            f"Публикации по годам: {years}.")


def prefilter(composition: Composition) -> tuple[Decision, str, str] | None:
    """Исключение до LLM по составу источников: (решение, код причины, объяснение) или None."""
    if composition.total == 0:
        return Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_NO_SOURCES", "Нет доступных источников для проверки."
    if composition.independent == 0:
        kinds = ", ".join(TYPE_RU.get(kind, kind) for kind in sorted(composition.by_type))
        return (Decision.HYPE_OR_NOISE, "RUBRIC_NO_INDEPENDENT_SOURCE",
                f"Нет независимого доверенного источника (только {kinds}): по ТЗ такие источники не могут быть "
                "единственным основанием для включения в выдачу.")
    if composition.review_share >= 0.5:
        return (Decision.INSUFFICIENT_EVIDENCE, "RUBRIC_REVIEWS",
                f"Больше половины источников — обзоры ({composition.reviews} из {composition.total}): "
                "это аналитика направления, а не конкретная технология.")
    return None


def pool_candidates(
    weak: Sequence[CandidateView], excluded: Sequence[CandidateView], limit: int
) -> tuple[list[CandidateView], list[CandidateView]]:
    """Пул для рубричной оценки: слабые сигналы analyzer и отсеянные только порогом его модели.

    Порог модели v1 не должен решать судьбу кандидата: на B v2 её ранжирование на уровне случайного
    (ROC-AUC 0,495), а все карточки с RSS отсекались до выдачи. Кандидаты, исключённые правилами analyzer,
    в пул не входят. Возвращает (пул, остальные).
    """
    by_model = sorted((item for item in excluded if item.decision_reason == "MODEL_SCORE"), key=lambda item: -item.score)
    ordered = sorted(weak, key=lambda item: item.rank) + by_model
    rest = [item for item in excluded if item.decision_reason != "MODEL_SCORE"]
    return ordered[:limit], ordered[limit:] + rest


def priority(verdict: JudgeVerdictView) -> int:
    """Балл как у организаторов: стадия + тренд (2…7)."""
    return verdict.stage + verdict.trend


def rank_selected(
    selected: Sequence[CandidateView], verdicts: dict[str, JudgeVerdictView]
) -> list[CandidateView]:
    """Порядок выдачи: балл стадия + тренд, затем уверенность, релевантность и исходный порядок."""
    position = {candidate.candidate_id: index for index, candidate in enumerate(selected)}
    return sorted(selected, key=lambda candidate: (
        -priority(verdicts[candidate.candidate_id]),
        -verdicts[candidate.candidate_id].confidence,
        -verdicts[candidate.candidate_id].relevance,
        position[candidate.candidate_id],
    ))


def rubric_features(
    verdict: JudgeVerdictView, composition: Composition, stage: int, trend: int
) -> list[tuple[str, str, float, float, FeatureDirection]]:
    """Ключевые предикторы режима rubric: критерии рубрики, стадия, тренд и состав источников."""
    def criterion(name: str, label: str, value: bool) -> tuple[str, str, float, float, FeatureDirection]:
        return (name, label, 1.0 if value else 0.0, 1.0 if value else -1.0,
                FeatureDirection.SUPPORTS_WEAK_SIGNAL if value else FeatureDirection.SUPPORTS_MATURE)

    return [
        criterion("rubric_on_topic", "Относится к запросу (рубрика LLM)", verdict.on_topic),
        criterion("rubric_concrete", "Конкретная технология, а не обзор или общее понятие", verdict.concrete),
        criterion("rubric_early_stage", "Ранняя стадия, не массовое внедрение", verdict.early_stage),
        criterion("rubric_verifiable", "Есть проверяемый независимый источник", verdict.verifiable),
        ("stage", "Стадия: 1 — исследование, 2 — прототип, 3 — пилот, 4 — раннее внедрение", float(stage),
         float(stage), FeatureDirection.NEUTRAL),
        ("trend", "Тренд: 1 — слабый интерес, 2 — растёт, 3 — растёт быстро", float(trend), float(trend),
         FeatureDirection.NEUTRAL),
        ("independent_sources", "Независимых доверенных источников", float(composition.independent),
         float(composition.independent), FeatureDirection.SUPPORTS_WEAK_SIGNAL),
        ("market_sources", "Рыночных источников (СМИ, аналитика, пресс-релизы)", float(composition.market),
         float(composition.market), FeatureDirection.NEUTRAL),
        ("review_share", "Доля обзоров среди источников", round(composition.review_share, 4),
         -round(composition.review_share, 4), FeatureDirection.SUPPORTS_MATURE),
        ("recent_share", "Доля публикаций двух последних лет", round(composition.recent_share, 4),
         round(composition.recent_share, 4), FeatureDirection.SUPPORTS_WEAK_SIGNAL),
    ]


def feature_rows(
    rubric: Sequence[tuple[str, str, float, float, FeatureDirection]], candidate: CandidateView
) -> tuple[FeatureRow, ...]:
    """Предикторы карточки: сначала рубрика и состав источников, затем признаки analyzer."""
    rows: list[FeatureRow] = []
    seen: set[str] = set()
    for name, label, value, contribution, direction in rubric:
        rows.append(FeatureRow(name, label, value, contribution, direction, len(rows) + 1))
        seen.add(name)
    for feature in candidate.features:
        if feature.feature_name in seen:
            continue
        rows.append(FeatureRow(feature.feature_name, feature.label_ru, feature.value, feature.contribution,
                               feature.direction, len(rows) + 1))
        seen.add(feature.feature_name)
    return tuple(rows)


def status_explanation(verdict: JudgeVerdictView, stage: int, trend: int) -> str:
    """Объяснение статуса слабого сигнала: критерии рубрики, стадия, тренд, балл и уверенность."""
    marks = " ".join(f"{label} {'✓' if value else '✗'};" for label, value in (
        ("по теме", verdict.on_topic), ("конкретно", verdict.concrete),
        ("ранняя стадия", verdict.early_stage), ("проверяемо", verdict.verifiable)))
    return (f"Рубрика: {marks} стадия {stage} ({STAGE_RU.get(stage, '—')}), тренд {trend} "
            f"({TREND_RU.get(trend, '—')}), балл {stage + trend} из 7; уверенность {verdict.confidence:.0%}. "
            f"{verdict.reason_ru}").strip()[:500]


def domain_of(url: str) -> str:
    """Площадка источника по ссылке (без www)."""
    host = urlsplit(url or "").hostname or ""
    return host.removeprefix("www.")
