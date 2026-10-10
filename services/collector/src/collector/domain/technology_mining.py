"""Извлечение названий технологий из документов широкого прохода (этап 4, доработка 4.2). Статистика, без LLM.

Широкий проход по фразам темы приносит свежие статьи, репозитории и новости. Названия конкретных технологий
повторяются в заголовках разных документов и источников. Правила этапа 4.2 — по замеру 07.10.2026, где извлечение
давало «show hn», «ai agents», «age of ai», «de la», обрывки вроде «card for time series»:
- заголовок режется по знакам препинания и тире: словосочетание не переходит через «:», «,», «—» и скобки;
- префиксы Hacker News («Show HN:», «Ask HN:», «Tell HN:», «Launch HN:») отбрасываются;
- кандидат — 2–4 английских слова без служебных слов внутри и по краям, последнее слово не «рамочное»;
- словосочетание из одних общих слов («ai agents», «agentic ai», «supply chain finance») — не технология;
- слово из частых романских и германских служебных слов («de», «la», «und») — неанглийский заголовок;
- нужен повтор минимум в двух разных документах из двух разных источников;
- единственное и множественное число склеиваются («optical interconnect» и «optical interconnects»).
Оценка: число документов и источников с поправкой на длину (как C-value); слишком частое (больше трети
документов) — сама тема; вложенный вариант поглощается длинным, если встречается не чаще его.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from collector.domain.technology_filters import (
    FRAGMENT_HEADS,
    FRAGMENT_LEADS,
    GENERIC_PHRASES,
    is_mature,
    is_verbish,
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]*[A-Za-z0-9+]|[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_SEGMENT_RE = re.compile(r"\s[-–—|]\s|[:;,.!?()\[\]{}\"“”«»/]|[–—]")
_HN_PREFIX_RE = re.compile(r"^\s*(?:show|ask|tell|launch)\s+hn\b\s*[:\-–—]?\s*", re.IGNORECASE)
STOP = frozenset("""a an the and or of for to in on at by with from via into over under as is are be its their our
this that these those using based towards toward through across within without between vs versus new novel
how what why when where which who can could should would may might do does we you they it not no more less
""".split())
TAIL_STOP = frozenset("""approach approaches framework frameworks study studies analysis survey review paper case
challenges opportunities perspective perspectives overview application applications results method methods
system systems model models solution solutions technique techniques tool tools platform platforms evaluation
benchmark design implementation use uses future trends report update news launch launches raises funding round
""".split())
# Частые служебные слова романских и германских языков: такой заголовок не англоязычный.
NON_ENGLISH = frozenset("""de la le les des du el los las del da dos das di il lo gli en et y und der die von zu mit
fur für per con por para na nos au aux une un ein eine""".split())
# Общие слова: словосочетание только из них (и служебных) — не технология, а общая тема.
GENERIC_TOKENS = frozenset("""ai artificial intelligence agent agents agentic model models llm llms gpt data cloud edge
local open source physical compute computing hardware server servers startup startups age era future security safety
inference generative machine learning deep neural system systems technology technologies tech digital smart platform
platforms solution solutions tool tools app apps application applications company companies industry industries
industrial world global human humans based powered driven native next infrastructure infrastructures supply chain
chains finance financial banking bank banks commercial enterprise enterprises business businesses market markets
management transformation inclusion service services operations customer customers product products research
development innovation innovations network networks internet online software computer computers robot robots robotics
automation automated autonomous intelligent manufacturing production factory factories energy power center centers
centre centres""".split())
GENERIC = frozenset({
    "artificial intelligence", "machine learning", "deep learning", "neural networks", "neural network",
    "large language models", "large language model", "language models", "language model", "generative ai",
    "edge computing", "cloud computing", "data center", "data centers", "internet of things", "big data",
    "computer vision", "natural language processing", "reinforcement learning", "digital twin", "digital twins",
})
MIN_DOCS = 2
MIN_SOURCES = 2
MAX_SHARE = 0.33


def _singular(word: str) -> str:
    """Единственное число английского слова по простым правилам (для склейки вариантов)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def phrase_key(phrase: str) -> str:
    """Ключ склейки: последнее слово — в единственном числе."""
    words = phrase.split()
    return " ".join([*words[:-1], _singular(words[-1])]) if words else phrase


def _segments(title: str) -> list[list[str]]:
    title = _HN_PREFIX_RE.sub("", title)
    return [[word.lower() for word in _TOKEN_RE.findall(part)] for part in _SEGMENT_RE.split(title)]


def _acceptable(gram: list[str]) -> bool:
    if gram[0] in STOP or gram[-1] in STOP or gram[-1] in TAIL_STOP:
        return False
    if any(word in STOP for word in gram[1:-1]):
        return False  # «card for time series», «safety in the age» — обрывки, а не названия
    if any(word in NON_ENGLISH or word == "hn" for word in gram):
        return False
    if any(len(word) < 2 or word.isdigit() for word in gram):
        return False
    return not all(word in GENERIC_TOKENS for word in gram)


def candidate_phrases(title: str) -> set[str]:
    """Словосочетания из 2–4 слов англоязычного заголовка (нижний регистр), не через знаки препинания.

    Этап 4.3: не обрывок общего термина (следующее слово не «learning», «networks»…), не на глаголе,
    не общая тема домена и не зрелая область.
    """
    if _CYRILLIC_RE.search(title or ""):
        return set()
    found: set[str] = set()
    for words in _segments(title or ""):
        for size in (2, 3, 4):
            for start in range(len(words) - size + 1):
                gram = words[start : start + size]
                if not _acceptable(gram) or is_verbish(gram):
                    continue
                following = words[start + size] if start + size < len(words) else ""
                previous = words[start - 1] if start > 0 else ""
                if following in FRAGMENT_HEADS or (gram[0] in ("intelligence", "learning") and previous in FRAGMENT_LEADS):
                    continue
                phrase = " ".join(gram)
                if phrase in GENERIC_PHRASES or is_mature(phrase):
                    continue
                found.add(phrase)
    return found


def mine_technologies(
    titles: Iterable[tuple[str, str]], exclude: Sequence[str] = (), limit: int = 15
) -> list[str]:
    """Технологии из заголовков `(заголовок, источник)` широкого прохода, по убыванию оценки, не больше `limit`."""
    rows = list(titles)
    docs: dict[str, set[int]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for number, (title, source) in enumerate(rows):
        for phrase in candidate_phrases(title or ""):
            key = phrase_key(phrase)
            docs[key].add(number)
            sources[key].add(source)
            surfaces[key][phrase] += 1
    total = max(1, len(rows))
    blocked = {phrase_key(" ".join(item.lower().split())) for item in exclude if item.strip()} | GENERIC
    scored = []
    for key, ids in docs.items():
        if len(ids) < MIN_DOCS or len(sources[key]) < MIN_SOURCES or len(ids) / total > MAX_SHARE:
            continue
        if key in blocked or any(key in other or other in key for other in blocked if " " in other):
            continue
        score = len(ids) * (1 + 0.5 * (len(sources[key]) - 1)) * math.log2(len(key.split()) + 1)
        scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[str] = []
    for _score, key in scored:
        if any(key in other and len(docs[key]) <= len(docs[other]) for other in chosen):
            continue  # вложенный вариант не чаще длинного — поглощён
        chosen = [other for other in chosen if not (other in key and len(docs[other]) <= len(docs[key]))]
        chosen.append(key)
        if len(chosen) >= limit:
            break
    return [surfaces[key].most_common(1)[0][0] for key in chosen]

DAYS_PER_MONTH = 30.44


def targeted_since(now: datetime, months: int, since_year: int) -> date:
    """Начало окна прицельного поиска: `months` месяцев назад, но не раньше 1 января `since_year`."""
    start = (now.astimezone(UTC) - timedelta(days=round(months * DAYS_PER_MONTH))).date()
    return max(start, date(since_year, 1, 1)) if since_year else start
