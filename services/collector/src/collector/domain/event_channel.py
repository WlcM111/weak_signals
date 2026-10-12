"""Канал «события стартапов» из архива новостей и отсев шаблонов новостей (этап 4.5). Статистика, без LLM.

Факты, на которых построен канал (данные экспертов и диагностика архива 11.10.2026):
- 49 из 66 экспертных статей на изданиях архива — события стартапов: в адресе raises, funding, seed, launches,
  unveils, stealth…;
- выборка по ключевым словам ФРАЗ ТЕМЫ (хотя бы 2) не взяла ни одной экспертной статьи даже в топ-500: у 55 %
  экспертных статей нет ни одного такого слова, хотя бы 2 — только у 9 %;
- со словами ТЕХНОЛОГИЙ, которые система уже отобрала (корпуса, извлечённые, модель), хотя бы 2 общих слова — у 34
  из 68 экспертных новостных адресов.
Поэтому словарь канала — ключевые слова темы и слова отобранных технологий, а порядок — по сумме «редкости» совпавших
слов внутри выборки (как IDF): конкретные слова («welding», «photonics») весят больше общих («robot», «center»).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date

from collector.domain.archive import ArchiveEntry
from collector.domain.technology_filters import GENERIC_TOPIC_WORDS, topic_keywords, with_variants
from collector.domain.text_tokens import tokens_of

EVENT_CUES = (
    # финансирование и запуск
    "raises", "raise", "raised", "raising", "funding", "funded", "seed", "series", "round", "secures", "closes",
    "launches", "launch", "launched", "unveils", "unveil", "unveiled", "debuts", "emerges", "stealth", "backed",
    "startup", "startups", "introduces", "acquires", "acquisition", "invests", "investment", "million",
    # этап 4.6 (аудит P20): внедрение, пилот, прототип, контракт, патент, сертификация, стандарт, открытый код
    "pilot", "pilots", "deploys", "deployed", "deployment", "prototype", "prototypes", "contract", "contracts",
    "patent", "patents", "certified", "certification", "standard", "standards", "open-source", "opensource",
    "releases", "partnership", "partners", "adopts", "selects", "awarded", "grant",
    # русские маркеры событий
    "привлекла", "привлек", "привлекает", "раунд", "раунде", "инвестиции", "инвестиций", "запустила", "запускает",
    "запуск", "представила", "представил", "пилот", "пилотный", "внедрила", "внедрение", "контракт", "патент",
    "прототип", "сертификат", "стандарт", "грант",
)
# Слова заголовков рыночных отчётов, пресс-релизов и анонсов мероприятий: словосочетание с ними из новостей —
# не технология («germany semiconductor memory market», «data center colocation databook», «reach us billion»).
NEWS_BOILERPLATE = frozenset("""market markets size share report reports forecast forecasts databook cagr billion million
nasdaq nyse insider summit conference webinar announces announced launches raises funding seed series round inc llc
ltd corp newswire press release outlook trends growth opportunities revenue demand mw gw area floor developer
construction investments hyperscale participants advisors expert details""".split())
MAX_VOCABULARY = 150


def base_form(word: str) -> str:
    """Основа слова для сопоставления форм числа (robots → robot, technologies → technology)."""
    return _base(word)


def is_all_caps(title: str) -> bool:
    """Заголовок набран прописными (патенты, пресс-релизы): «аббревиатуры» в нём — обычные слова (этап 4.7)."""
    letters = [ch for ch in title or "" if ch.isalpha()]
    return len(letters) >= 8 and sum(ch.isupper() for ch in letters) / len(letters) > 0.8


def _base(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def event_vocabulary(topic_phrases: Sequence[str], technologies: Sequence[str]) -> list[str]:
    """Словарь канала: ключевые слова темы и значимые слова отобранных технологий (обе формы числа)."""
    words: dict[str, None] = dict.fromkeys(topic_keywords(topic_phrases, limit=40))
    for phrase in technologies:
        for word in tokens_of(phrase):
            if len(word) >= 3 and word not in GENERIC_TOPIC_WORDS and not word.isdigit():
                words.setdefault(word, None)
    return with_variants(list(words)[:MAX_VOCABULARY])


def rank_events(
    candidates: Iterable[tuple[ArchiveEntry, Sequence[str]]], vocabulary: Sequence[str], min_overlap: int, limit: int
) -> list[tuple[ArchiveEntry, list[str]]]:
    """Статьи с не меньше `min_overlap` словами словаря (по основе слова), по убыванию суммы редкости совпавших слов.

    Редкость = log((n + 1) / (df + 0.5)), где df — в скольких кандидатах выборки есть слово (по всей выборке, а не
    только среди прошедших порог); при равенстве — новее. Возвращает статьи и совпавшие слова, самые редкие первыми.
    """
    bases = {_base(word) for word in vocabulary}
    matched_rows: list[tuple[ArchiveEntry, set[str]]] = []
    df: Counter[str] = Counter()
    for entry, tokens in candidates:
        matched = {_base(token) for token in tokens} & bases
        if matched:
            matched_rows.append((entry, matched))
            df.update(matched)
    n = len(matched_rows)
    scored = []
    for entry, matched in matched_rows:
        if len(matched) < min_overlap:
            continue
        score = sum(math.log((n + 1) / (df[word] + 0.5)) for word in matched)
        scored.append((score, (entry.published_on or date.min).toordinal(), entry, matched))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].url))
    return [(entry, sorted(matched, key=lambda word: (df[word], word))) for _, _, entry, matched in scored[:limit]]


def is_news_boilerplate(term: str) -> bool:
    """Словосочетание из шаблона новости: рыночный отчёт, пресс-релиз, мероприятие."""
    return any(word in NEWS_BOILERPLATE for word in term.lower().split())


_ENTITY = re.compile(
    r"\b(?P<name>[A-Z][\w&.'’-]*(?:\s+[A-Z0-9][\w&.'’-]*){0,3})\s+(?:raises|raised|launches|unveils|emerges|secures|"
    r"closes|debuts|introduces|announces|acquires|lands|nabs|bags|gets)\b")
# Этап 5.0 (аудит round2 F20): русские заголовки событий («РобоТех представила…»).
_ENTITY_RU = re.compile(
    r"(?<![\w])(?P<name>[А-ЯЁ][\wЁё&.'’-]*(?:\s+[А-ЯЁA-Z0-9][\wЁё&.'’-]*){0,3})\s+(?:привлекла|привлек|запустила|"
    r"запускает|представила|представил|получила|открыла|выпустила|анонсировала|объявила)(?![\w])")
_NOT_ENTITY = frozenset("""the a an new this startup startups company report exclusive breaking update ai why how what
компания стартап новая новый""".split()) | frozenset("""the a an new this startup startups company report exclusive breaking update ai why how what
week today""".split())


def extract_entities(titles: Iterable[str], limit: int = 20) -> list[str]:
    """Этап 4.6 (аудит P22): компании и продукты из настоящих заголовков событий («Keycard raises…»,
    «Tetragen Robotics launches…»); по убыванию числа заголовков. Названия от модели их не заменяют."""
    counts: Counter[str] = Counter()
    for title in titles:
        if is_all_caps(title):
            continue  # этап 4.7: в заголовке прописными каждое слово похоже на название
        for match in [*_ENTITY.finditer(title or ""), *_ENTITY_RU.finditer(title or "")]:
            name = " ".join(match.group("name").split())
            words = name.lower().split()
            if words and words[0] not in _NOT_ENTITY and len(name) >= 3:
                counts[name] += 1
    return [name for name, _count in counts.most_common(limit)]
