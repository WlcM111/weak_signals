"""Экстрактивный резерв: нарратив и резюме без обращения к LLM (§6 HANDOFF).

Требование ТЗ — не формировать выдачу на знаниях языковой модели без подтверждённого поиска.
Здесь верно и обратное: когда модели нет, выдача всё равно собирается, но только из текстов
доказательств, и явно помечается `FALLBACK_EXTRACTIVE`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from insight.domain.entities import (
    CandidateContext,
    EvidenceDocument,
    GroundingCheck,
    Narrative,
    SourceSummary,
)
from insight.domain.grounding import cyrillic_share
from insight.domain.values import (
    MAX_CASE_EXAMPLE,
    MAX_DESCRIPTION,
    MAX_EXPLANATION,
    MAX_SUMMARY,
    MAX_TITLE,
    SummaryKind,
)

_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]?", re.UNICODE)
ADVANTAGE_PLACEHOLDER = (
    "Не сформулировано автоматически: генеративная модель недоступна, см. источники ниже."
)
DESCRIPTION_DOCUMENTS = 2
SUMMARY_SENTENCES = 2
EXPLANATION_FEATURES = 2
LATIN_TITLE_PREFIX = "Технология: "


def first_sentences(text: str, count: int, max_length: int) -> str:
    """Первые `count` предложений, укладывающиеся в лимит длины."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    sentences = [item.strip() for item in _SENTENCE_RE.findall(cleaned) if item.strip()]
    result = ""
    for sentence in sentences[:count]:
        candidate = f"{result} {sentence}".strip()
        if len(candidate) > max_length:
            break
        result = candidate
    return result or cleaned[:max_length].rstrip()


def extractive_title(candidate: CandidateContext) -> str:
    """Название кандидата; латиница получает русский префикс, чтобы выдача была на русском."""
    title = " ".join(candidate.title.split())[:MAX_TITLE]
    if title and cyrillic_share(title) < 0.5:
        return f"{LATIN_TITLE_PREFIX}{title}"[:MAX_TITLE]
    return title


def extractive_description(evidence: Sequence[EvidenceDocument]) -> str:
    """Описание из сниппетов двух лучших доказательств: русские источники приоритетны."""
    ordered = sorted(
        evidence, key=lambda document: (not document.is_russian, -document.trust_level.rank)
    )
    parts: list[str] = []
    for document in ordered[:DESCRIPTION_DOCUMENTS]:
        piece = first_sentences(document.text, SUMMARY_SENTENCES, MAX_DESCRIPTION // 2)
        if piece:
            parts.append(piece)
    return " ".join(parts)[:MAX_DESCRIPTION]


def extractive_case_example(evidence: Sequence[EvidenceDocument]) -> str:
    """Кейс-пример: заголовок самого доверенного документа и его домен."""
    if not evidence:
        return "Кейс-пример недоступен: доказательные документы не переданы."
    best = max(evidence, key=lambda document: document.trust_level.rank)
    return f"{best.title} ({best.origin_domain})"[:MAX_CASE_EXAMPLE]


def extractive_explanation(candidate: CandidateContext) -> str:
    """Объяснение по двум главным признакам analyzer, без интерпретации."""
    features = sorted(
        candidate.top_features, key=lambda item: abs(item.contribution), reverse=True
    )[:EXPLANATION_FEATURES]
    if not features:
        return (
            f"Решение получено моделью analyzer с оценкой {candidate.score:.2f}; "
            "признаки не переданы."
        )[:MAX_EXPLANATION]
    parts = ", ".join(
        f"{feature.label_ru.lower()} = {feature.value:.2f} ({feature.direction.label_ru})"
        for feature in features
    )
    return (
        f"Оценка модели {candidate.score:.2f}. Признаки: {parts}. "
        "Текст собран из источников без генеративной модели."
    )[:MAX_EXPLANATION]


def extractive_narrative(
    candidate: CandidateContext, evidence: Sequence[EvidenceDocument]
) -> Narrative:
    """Полный экстрактивный нарратив кандидата."""
    return Narrative(
        title_ru=extractive_title(candidate) or "Без названия",
        description_ru=extractive_description(evidence),
        advantage_ru=ADVANTAGE_PLACEHOLDER,
        case_example_ru=extractive_case_example(evidence),
        explanation_ru=extractive_explanation(candidate),
        case_document_id="",
    )


def extractive_summaries(evidence: Sequence[EvidenceDocument]) -> tuple[SourceSummary, ...]:
    """Резюме источников без генерации: русские — как есть, иноязычные — первые предложения."""
    return tuple(
        SourceSummary(
            document_id=document.document_id,
            summary_ru=first_sentences(document.text, SUMMARY_SENTENCES, MAX_SUMMARY)
            or document.title[:MAX_SUMMARY],
            kind=SummaryKind.ORIGINAL_RU if document.is_russian else SummaryKind.EXTRACTIVE,
            position=position,
        )
        for position, document in enumerate(evidence, start=1)
    )


def fallback_grounding(reason: str = "") -> GroundingCheck:
    """Проверка обоснованности для экстрактивного результата: он собран из источников."""
    return GroundingCheck(
        passed=True,
        unsupported_numbers=0,
        unknown_document_refs=0,
        features_mentioned=0,
        soft_failures=(reason,) if reason else (),
    )
