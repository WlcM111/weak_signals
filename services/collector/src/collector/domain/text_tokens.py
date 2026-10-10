"""Слова текста для локальных индексов (архив новостей, корпус arXiv) и фразы запроса для поиска по ним (этап 4.3).

Индекс хранит множество значимых слов документа (`tokens`). Фраза находит документ, если в нём есть все её
значимые слова (`tokens @> слова фразы` в PostgreSQL, индекс GIN). Регистр не важен, «ё» = «е», служебные слова
английского и русского языков не индексируются.
"""

from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
STOP_WORDS = frozenset("""a an the and or of for to in on at by with from via into over under as is are be was were been
its it this that these those their our your his her them they we you i not no nor but if then than so such can could
should would may might will shall do does did done how what why when where which who whom whose new also more most
less very just about after before up out off all any each other some own same too only per vs versus
и в во на с со к ко по о об от до из за для при над под про без через или а но же ли не ни что как это этот эта эти
""".split())
MAX_PHRASE_WORDS = 5


def tokens_of(text: str, limit: int = 120) -> list[str]:
    """Значимые слова текста без повторов, в порядке появления, не больше `limit`."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    seen: dict[str, None] = {}
    for word in _WORD_RE.findall(normalized):
        if len(word) >= 2 and word not in STOP_WORDS:
            seen.setdefault(word, None)
            if len(seen) >= limit:
                break
    return list(seen)


def phrase_words(phrase: str) -> list[str]:
    """Значимые слова фразы запроса для поиска «все слова»; длинная фраза — первые пять значимых слов."""
    return tokens_of(phrase, limit=MAX_PHRASE_WORDS)


def slug_title(path: str) -> str:
    """Заголовок из адреса статьи: последний информативный участок пути, дефисы и подчёркивания — пробелы."""
    parts = [part for part in re.split(r"/+", path or "") if part]
    parts = [re.sub(r"\.(html?|php|aspx?)$", "", part, flags=re.IGNORECASE) for part in parts]
    words = [part for part in parts if re.search(r"[a-zа-я]", part, re.IGNORECASE)]
    best = max(words, key=lambda part: part.count("-") + part.count("_"), default="")
    return " ".join(re.split(r"[-_]+", best)).strip()
