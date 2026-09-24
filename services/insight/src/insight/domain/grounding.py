"""Проверка обоснованности нарратива переданными доказательствами (§6 HANDOFF).

Ответ языковой модели проверяется как недоверенные данные: числа обязаны встречаться в текстах
доказательств, кейс-пример — ссылаться на переданный документ, ссылки `[doc:N]` — существовать,
объяснение — опираться на признаки analyzer, а язык выдачи — быть русским. Это же защищает от
инъекций через тексты источников: что бы в них ни было написано, наружу выходит только то,
что прошло эти проверки.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from insight.domain.entities import EvidenceDocument, FeatureContribution, GroundingCheck, Narrative

YEAR_MIN = 2000
YEAR_MAX = 2030
TRIVIAL_NUMBER_MAX = 2.0
MIN_STEM_LENGTH = 6
DOC_REF_RE = re.compile(r"\[doc:(\d+)\]", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_SPACE_RE = re.compile(r"[\s\u00a0]+")


def normalize_number(raw: str) -> float | None:
    """Строковое число → float; `1 234,5` и `1.234,5` не различаются по разделителю."""
    cleaned = raw.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    """Числа, которые обязаны подтверждаться доказательствами.

    Не учитываются годы 2000–2030 (их модель может назвать из даты публикации) и числа ≤ 2
    (порядковые «две компании», «оба источника»): требовать для них подтверждения — шум.
    """
    numbers: list[float] = []
    for match in NUMBER_RE.finditer(text):
        value = normalize_number(match.group(0))
        if value is None:
            continue
        if value.is_integer() and YEAR_MIN <= value <= YEAR_MAX:
            continue
        if abs(value) <= TRIVIAL_NUMBER_MAX:
            continue
        numbers.append(value)
    return numbers


def numbers_in_evidence(evidence: Sequence[EvidenceDocument]) -> set[float]:
    """Все числа, встречающиеся в доказательствах (включая заголовки)."""
    found: set[float] = set()
    for document in evidence:
        for match in NUMBER_RE.finditer(f"{document.title} {document.text}"):
            value = normalize_number(match.group(0))
            if value is not None:
                found.add(value)
                found.add(round(value, 2))
    return found


def unsupported_numbers(
    text: str, evidence: Sequence[EvidenceDocument], allowed: frozenset[float] = frozenset()
) -> list[float]:
    """Числа нарратива, которых нет ни в доказательствах, ни среди разрешённых входных чисел.

    Ссылки `[doc:N]` вырезаются до поиска чисел: номер документа — служебная пометка, а не факт.
    Без этого любой `[doc:3]`…`[doc:8]` засчитывался как «число, отсутствующее в источниках»,
    и корректный ответ модели уходил в экстрактивный резерв.
    """
    supported = numbers_in_evidence(evidence) | allowed
    return [
        value
        for value in extract_numbers(strip_doc_refs(text))
        if value not in supported and round(value, 2) not in supported
    ]


def model_input_numbers(features: Sequence[FeatureContribution], score: float | None = None) -> frozenset[float]:
    """Числа, которые модель получила во входе и которые объяснение обязано цитировать.

    Промпт требует назвать значения признаков, а в `top_features` они передаются с округлением до
    четырёх знаков; оценка кандидата может быть названа и в процентах. Вклады признаков модели не
    передаются, поэтому и цитировать их нельзя. Разрешение действует только для explanation_ru:
    в описании, преимуществе и кейсе допустимы лишь числа из источников.
    """
    forms: set[float] = set()
    values = [feature.value for feature in features] + ([score] if score is not None else [])
    for value in values:
        forms.update({value, round(value, 1), round(value, 2), round(value, 3), round(value, 4)})
        if 0.0 <= value <= 1.0:
            percent = value * 100
            forms.update({float(round(percent)), round(percent, 1), round(percent, 2)})
    return frozenset(forms)


def unknown_document_refs(text: str, evidence_count: int) -> list[int]:
    """Ссылки `[doc:N]` вне диапазона 1..N."""
    return [
        int(match.group(1))
        for match in DOC_REF_RE.finditer(text)
        if not 1 <= int(match.group(1)) <= evidence_count
    ]


def strip_doc_refs(text: str) -> str:
    """Текст без служебных ссылок `[doc:N]` — то, что увидит пользователь."""
    return _SPACE_RE.sub(" ", DOC_REF_RE.sub(" ", text)).strip()


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв текста; 1.0 для текста без букв."""
    letters = LETTER_RE.findall(text)
    if not letters:
        return 1.0
    cyrillic = sum(1 for letter in letters if CYRILLIC_RE.match(letter))
    return cyrillic / len(letters)


def count_mentioned_features(text: str, features: Sequence[FeatureContribution]) -> int:
    """Сколько признаков упомянуто в объяснении.

    Сравнение идёт по основам слов метки (первые 6 символов): «доля научных публикаций» находится
    и в форме «доли научных публикаций», без морфологического анализатора.
    """
    lowered = text.lower()
    mentioned = 0
    for feature in features:
        words = [word for word in re.findall(r"[\w-]+", feature.label_ru.lower()) if len(word) >= 3]
        if not words:
            continue
        significant = [word for word in words if len(word) >= MIN_STEM_LENGTH] or words
        if all(word[:MIN_STEM_LENGTH] in lowered for word in significant):
            mentioned += 1
    return mentioned


def check(
    narrative: Narrative,
    evidence: Sequence[EvidenceDocument],
    features: Sequence[FeatureContribution],
    min_features: int = 2,
    min_cyrillic_share: float = 0.6,
    score: float | None = None,
) -> GroundingCheck:
    """Полная проверка нарратива; жёсткие и мягкие нарушения возвращаются раздельно."""
    hard: list[str] = []
    soft: list[str] = []
    whole_text = " ".join(
        (
            narrative.title_ru,
            narrative.description_ru,
            narrative.advantage_ru,
            narrative.case_example_ru,
            narrative.explanation_ru,
        )
    )
    factual_text = " ".join(
        (narrative.title_ru, narrative.description_ru, narrative.advantage_ru, narrative.case_example_ru)
    )
    bad_numbers = unsupported_numbers(factual_text, evidence) + unsupported_numbers(
        narrative.explanation_ru, evidence, model_input_numbers(features, score)
    )
    if bad_numbers:
        hard.append(
            "числа, отсутствующие в источниках: "
            + ", ".join(_format_number(value) for value in bad_numbers[:5])
        )
    bad_refs = unknown_document_refs(whole_text, len(evidence))
    if bad_refs:
        hard.append("ссылки на несуществующие документы: " + ", ".join(str(ref) for ref in bad_refs[:5]))
    known_documents = {document.document_id for document in evidence}
    if narrative.case_document_id and narrative.case_document_id not in known_documents:
        hard.append("case_document_id не входит в переданные доказательства")
    if not narrative.case_document_id:
        soft.append("кейс-пример не привязан к документу")

    mentioned = count_mentioned_features(narrative.explanation_ru, features)
    if features and mentioned < min_features:
        soft.append(
            f"в объяснении упомянуто признаков {mentioned}, требуется не менее {min_features}"
        )
    for name, text in (
        ("description_ru", narrative.description_ru),
        ("advantage_ru", narrative.advantage_ru),
        ("explanation_ru", narrative.explanation_ru),
    ):
        if cyrillic_share(text) < min_cyrillic_share:
            soft.append(f"{name}: текст не на русском языке")
    return GroundingCheck(
        passed=not hard and not soft,
        unsupported_numbers=len(bad_numbers),
        unknown_document_refs=len(bad_refs),
        features_mentioned=mentioned,
        hard_failures=tuple(hard),
        soft_failures=tuple(soft),
    )


def _format_number(value: float) -> str:
    """Число для сообщения об ошибке без хвостовых нулей."""
    return str(int(value)) if value.is_integer() else f"{value:g}"
