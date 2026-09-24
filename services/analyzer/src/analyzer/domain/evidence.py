"""Отбор доказательств кандидата и построение сниппетов (§12.5, шаг 11).

Вынесено в отдельный модуль домена: правила отбора не относятся ни к признакам, ни к кластеризации,
а их корректность (гарантия доверенного источника, лимит длины) проверяется отдельно.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from analyzer.domain.entities import MAX_SNIPPET_LENGTH, DocumentRef
from analyzer.domain.values import TrustLevel

_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]?", re.UNICODE)
TRUSTED_LEVELS = (TrustLevel.HIGH, TrustLevel.MEDIUM)


def select_evidence(
    documents: Sequence[DocumentRef], similarities: Sequence[float], max_items: int
) -> list[int]:
    """Индексы доказательств: топ по близости к центроиду с гарантией доверенного источника.

    Если среди документов кластера есть HIGH или MEDIUM, хотя бы один такой документ обязательно
    попадает в доказательства — иначе выдача опиралась бы только на источники низкой доверенности.
    """
    if not documents or max_items <= 0:
        return []
    order = sorted(range(len(documents)), key=lambda index: (-similarities[index], index))
    selected = order[:max_items]
    trusted_exists = any(document.trust_level in TRUSTED_LEVELS for document in documents)
    if not trusted_exists or any(documents[index].trust_level in TRUSTED_LEVELS for index in selected):
        return selected
    best_trusted = next(index for index in order if documents[index].trust_level in TRUSTED_LEVELS)
    selected[-1] = best_trusted
    return sorted(selected, key=lambda index: (-similarities[index], index))


def build_snippet(text: str, keyphrases: Sequence[str], max_length: int = MAX_SNIPPET_LENGTH) -> str:
    """Первые предложения, содержащие ключевую фразу; иначе начало текста. Длина ≤ 600 символов."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    sentences = [sentence.strip() for sentence in _SENTENCE_RE.findall(cleaned) if sentence.strip()]
    lowered_phrases = [phrase.lower() for phrase in keyphrases if phrase]
    start = 0
    for position, sentence in enumerate(sentences):
        lowered = sentence.lower()
        if any(phrase in lowered for phrase in lowered_phrases):
            start = position
            break
    snippet = ""
    for sentence in sentences[start:]:
        candidate = f"{snippet} {sentence}".strip()
        if len(candidate) > max_length:
            break
        snippet = candidate
    if not snippet:
        snippet = cleaned[:max_length].rstrip()
    return snippet[:max_length]
