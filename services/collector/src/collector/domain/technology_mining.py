"""Извлечение названий технологий из документов широкого прохода (этап 4). Статистика, без LLM.

Широкий проход по фразам темы приносит свежие статьи, репозитории и новости. Названия конкретных технологий
в них повторяются в заголовках разных документов и источников — «optical circuit switching», «KV cache
offloading». Кандидаты — словосочетания из 2–4 слов в англоязычных заголовках; граничные слова не служебные,
последнее слово не «рамочное» («approach», «survey»). Оценка — число разных документов и источников с этим
словосочетанием, с поправкой на длину (как C-value: длинное специфичное словосочетание ценнее). Слишком частые
словосочетания (больше трети документов) — сама тема, а не технология внутри неё; вложенные варианты
поглощаются более длинным, если встречаются не чаще его.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]*[A-Za-z0-9+]|[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
STOP = frozenset("""a an the and or of for to in on at by with from via into over under as is are be its their our
this that these those using based towards toward through across within without between vs versus new novel
how what why when where which who can could should would may might do does we you they it not no more less
""".split())
TAIL_STOP = frozenset("""approach approaches framework frameworks study studies analysis survey review paper case
challenges opportunities perspective perspectives overview application applications results method methods
system systems model models solution solutions technique techniques tool tools platform platforms evaluation
benchmark design implementation use uses future trends report update news launch launches raises funding round
""".split())
GENERIC = frozenset({
    "artificial intelligence", "machine learning", "deep learning", "neural networks", "neural network",
    "large language models", "large language model", "language models", "language model", "generative ai",
    "edge computing", "cloud computing", "data center", "data centers", "internet of things", "big data",
    "computer vision", "natural language processing", "reinforcement learning", "digital twin", "digital twins",
})
MIN_DOCS = 2
MAX_SHARE = 0.33


def _tokens(title: str) -> list[str]:
    return _TOKEN_RE.findall(title)


def candidate_phrases(title: str) -> set[str]:
    """Словосочетания из 2–4 слов англоязычного заголовка (нижний регистр)."""
    if _CYRILLIC_RE.search(title):
        return set()
    words = [word.lower() for word in _tokens(title)]
    found: set[str] = set()
    for size in (2, 3, 4):
        for start in range(len(words) - size + 1):
            gram = words[start : start + size]
            if gram[0] in STOP or gram[-1] in STOP or gram[-1] in TAIL_STOP:
                continue
            if any(len(word) < 2 for word in gram) or all(word.isdigit() for word in gram):
                continue
            found.add(" ".join(gram))
    return found


def mine_technologies(
    titles: Iterable[tuple[str, str]], exclude: Sequence[str] = (), limit: int = 15
) -> list[str]:
    """Технологии из заголовков `(заголовок, источник)` широкого прохода, по убыванию оценки, не больше `limit`."""
    rows = list(titles)
    docs: dict[str, set[int]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    for number, (title, source) in enumerate(rows):
        for phrase in candidate_phrases(title or ""):
            docs[phrase].add(number)
            sources[phrase].add(source)
    total = max(1, len(rows))
    blocked = {" ".join(item.lower().split()) for item in exclude} | GENERIC
    scored = []
    for phrase, ids in docs.items():
        if len(ids) < MIN_DOCS or len(ids) / total > MAX_SHARE or phrase in blocked:
            continue
        if any(phrase in other or other in phrase for other in blocked if " " in other):
            continue
        score = len(ids) * (1 + 0.5 * (len(sources[phrase]) - 1)) * math.log2(len(phrase.split()) + 1)
        scored.append((score, phrase))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[str] = []
    for _score, phrase in scored:
        if any(phrase in other and len(docs[phrase]) <= len(docs[other]) for other in chosen):
            continue  # вложенный вариант не чаще длинного — поглощён
        chosen = [other for other in chosen if not (other in phrase and len(docs[other]) <= len(docs[phrase]))]
        chosen.append(phrase)
        if len(chosen) >= limit:
            break
    return chosen


DAYS_PER_MONTH = 30.44


def targeted_since(now: datetime, months: int, since_year: int) -> date:
    """Начало окна прицельного поиска: `months` месяцев назад, но не раньше 1 января `since_year`."""
    start = (now.astimezone(UTC) - timedelta(days=round(months * DAYS_PER_MONTH))).date()
    return max(start, date(since_year, 1, 1)) if since_year else start
