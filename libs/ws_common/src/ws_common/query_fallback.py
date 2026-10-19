"""Резервное построение поисковых фраз без LLM (общая логика insight и orchestrator).

Запрос пользователя — фраза на естественном языке («слабые сигналы в области кибербезопасности»).
Поисковым API нужны содержательные термины, причём англоязычным источникам — на английском.
Модуль убирает служебные слова и «рамку» формулировки, переводит содержательные слова по
глоссарию с учётом словоформ и возвращает несколько коротких фраз вместо одной длинной.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MAX_FALLBACK_TERMS = 6
MIN_STEM_WORD = 5
# R-049, строгий режим: у статьи за общей основой только окончание (до трёх букв); у слова длиннее —
# только производное от основы не короче шести букв («финансовые» ← «финансы»)
MAX_ENDING = 3
MIN_DERIVED_STEM = 6
_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Предлоги, союзы и слова-«рамка» запроса: они описывают задачу поиска, а не предметную область.
STOP_WORDS = frozenset(
    """
    в во на для по и или с со к ко о об от из у за при над под как что это а но же ли бы
    слабые слабых слабый слабым сигналы сигналов сигнал сигналам
    область области областей сфера сфере сферы отрасль отрасли отраслях направление направлении направления
    перспективные перспективных перспективная перспективное новые новых новейшие новейших
    зарождающиеся зарождающихся ранние ранних будущие будущих
    решения решений решение тренды трендов тренд технологии технологий технология технологиях
    разработки разработок разработка
    weak signal signals emerging trend trends in of for the and on new
    """.split()
)


def _common_prefix(first: str, second: str) -> int:
    """Длина общего префикса двух слов."""
    length = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        length += 1
    return length


def _same_lexeme(word: str, key: str, strict: bool = False) -> bool:
    """Одна ли это лексема с точностью до окончания: «финтехе» ~ «финтех», «квантовых» ~ «квантовые».

    strict (R-049): основа статьи целиком входит в слово, длинное продолжение слова — только от длинной
    основы: «медицинские» ~ «медицина», но «медиа» ≁ «медицина» и «медицина» ≁ «медиа».
    """
    if word == key:
        return True
    if len(word) < MIN_STEM_WORD or len(key) < MIN_STEM_WORD:
        return False
    common = _common_prefix(word, key)
    if strict and (
        len(key) - common > MAX_ENDING or (len(word) - common > MAX_ENDING and common < MIN_DERIVED_STEM)
    ):
        return False
    return common >= max(4, min(len(word), len(key)) - 2)


def _split_hyphenated(words: list[str]) -> list[str]:
    """Строгий режим: слово с дефисом и кириллицей — по частям («edge-вычисления» → edge, вычисления)."""
    parts: list[str] = []
    for word in words:
        if "-" in word and _CYRILLIC_RE.search(word):
            parts.extend(part for part in word.split("-") if part)
        else:
            parts.append(word)
    return parts


def content_segments(query_text: str) -> list[list[str]]:
    """Группы подряд идущих содержательных слов запроса; служебные слова служат разделителями."""
    segments: list[list[str]] = []
    current: list[str] = []
    for word in _WORD_RE.findall(query_text.lower()):
        if word in STOP_WORDS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(word)
    if current:
        segments.append(current)
    if not segments:  # запрос целиком из «рамочных» слов («технологии»): фильтр не применяется
        words = _WORD_RE.findall(query_text.lower())
        return [words] if words else []
    return segments


def _translate_segment(
    words: list[str], terms: Mapping[str, str], strict: bool = False, missed: list[str] | None = None
) -> list[str]:
    """Перевод группы слов: сначала многословные статьи глоссария, затем отдельные слова.

    В `missed` (если передан) складываются кириллические слова без перевода.
    """
    if strict:
        words = _split_hyphenated(words)
    phrase_keys = [key.split() for key in terms if " " in key]
    single_keys = [key for key in terms if " " not in key]
    result: list[str] = []
    position = 0
    while position < len(words):
        matched = False
        for key_words in sorted(phrase_keys, key=len, reverse=True):
            window = words[position : position + len(key_words)]
            if len(window) == len(key_words) and all(
                _same_lexeme(word, key, strict=strict) for word, key in zip(window, key_words, strict=True)
            ):
                result.append(terms[" ".join(key_words)])
                position += len(key_words)
                matched = True
                break
        if matched:
            continue
        word = words[position]
        position += 1
        if not _CYRILLIC_RE.search(word):
            result.append(word)  # латиница и аббревиатуры (edge, llm, iot) остаются как есть
            continue
        candidates = [key for key in single_keys if _same_lexeme(word, key, strict=strict)]
        if candidates:
            best = max(candidates, key=lambda key: (_common_prefix(word, key), -abs(len(key) - len(word))))
            result.append(terms[best])
        elif missed is not None:
            missed.append(word)
        # кириллическое слово без перевода в английскую фразу не попадает
    return result


def _lowered(terms: Mapping[str, str], strict: bool) -> dict[str, str]:
    """Ключи словаря в нижнем регистре; в строгом режиме статьи с дефисом делятся, как слова запроса."""
    if strict:
        return {" ".join(_split_hyphenated(key.lower().split())): value for key, value in terms.items()}
    return {key.lower(): value for key, value in terms.items()}


def _unique(items: list[str], limit: int) -> tuple[str, ...]:
    """Непустые фразы без повторов в исходном порядке."""
    seen: dict[str, str] = {}
    for item in items:
        cleaned = " ".join(item.split())
        if len(cleaned) >= 2 and cleaned.lower() not in seen:
            seen[cleaned.lower()] = cleaned
    return tuple(list(seen.values())[:limit])


def fallback_terms(
    query_text: str, terms: Mapping[str, str], strict: bool = False
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Русские и английские поисковые фразы для запроса без обращения к LLM.

    Возвращает `(ru_terms, en_terms)`; каждая сторона непуста: когда перевести нечего,
    английской фразой остаётся сам запрос (прежнее поведение).
    strict (R-049, WS_QUERY_FALLBACK_STRICT): строгое совпадение основы, слова с дефисом — по частям;
    русские фразы от режима не зависят.
    """
    lowered = _lowered(terms, strict)
    segments = content_segments(query_text)
    ru_candidates = [" ".join(segment) for segment in segments]
    translated = [_translate_segment(segment, lowered, strict) for segment in segments]
    en_segments = [" ".join(words) for words in translated if words]
    combined_en = " ".join(en_segments)
    combined_ru = " ".join(ru_candidates)
    ru_terms = _unique([combined_ru, *_specific(ru_candidates), query_text], MAX_FALLBACK_TERMS)
    en_terms = _unique([combined_en, *_specific(en_segments)], MAX_FALLBACK_TERMS)
    return ru_terms or (query_text,), en_terms or (query_text,)


def untranslated_words(query_text: str, terms: Mapping[str, str], strict: bool = False) -> tuple[str, ...]:
    """Содержательные кириллические слова запроса без статьи словаря — в английские фразы они не попали;
    вызывающий пишет их в журнал, чтобы обеднение темы было видно (R-049)."""
    lowered = _lowered(terms, strict)
    missed: list[str] = []
    for segment in content_segments(query_text):
        _translate_segment(segment, lowered, strict, missed)
    return tuple(dict.fromkeys(missed))


def _specific(segments: list[str]) -> list[str]:
    """Многословные фразы, если они есть: одиночные слова («medicine», «industry») слишком общие
    и расходуют лимиты источников на нерелевантную выдачу."""
    multiword = [segment for segment in segments if len(segment.split()) >= 2]
    return multiword or segments