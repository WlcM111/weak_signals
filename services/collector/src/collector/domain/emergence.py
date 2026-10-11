"""Оценка возникновения технологий по корпусам заголовков (этап 4.3). Статистика, без LLM.

Кандидаты — словосочетания заголовков (те же правила, что у извлечения: `candidate_phrases`). Признак слабого
сигнала — быстрый рост доли документов с этим словосочетанием в недавнем периоде при малом объёме:
- недавний период и базовый задаются датами (корпус arXiv: 2025–2026 против 2021–2023; новости: последние
  6 месяцев против предыдущих 12);
- доля = документы с фразой / все документы периода (корпус выбирается по фразам темы, поэтому доля — внутри темы);
- рост = (доля недавно + сглаживание) / (доля в базе + сглаживание); оценка = рост × log2(1 + документов недавно);
- отсев: меньше `min_recent` документов недавно; доля недавно выше `max_share` — это сама тема, а не технология
  внутри неё; общие темы и зрелые области (`technology_filters`).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from collector.domain.technology_mining import candidate_phrases, phrase_key


@dataclass(frozen=True)
class EmergingTerm:
    """Кандидат-технология с оценкой возникновения."""

    term: str
    recent: int
    base: int
    score: float


def emerging_terms(
    rows: Iterable[tuple[str, date | None]],
    *,
    recent_from: date,
    base_from: date,
    base_to: date,
    exclude: Sequence[str] = (),
    limit: int = 25,
    min_recent: int = 3,
    max_share: float = 0.2,
    min_growth: float = 1.5,
    min_base_total: int = 0,
) -> list[EmergingTerm]:
    """Кандидаты по убыванию оценки возникновения, не больше `limit`."""
    recent: Counter[str] = Counter()
    base: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    total_recent = total_base = 0
    for title, day in rows:
        if day is None:
            continue
        in_recent = day >= recent_from
        in_base = base_from <= day <= base_to
        if not (in_recent or in_base):
            continue
        keys = {}
        for phrase in candidate_phrases(title or ""):
            keys[phrase_key(phrase)] = phrase
        if in_recent:
            total_recent += 1
            recent.update(keys.keys())
        else:
            total_base += 1
            base.update(keys.keys())
        for key, phrase in keys.items():
            surfaces[key][phrase] += 1
    # этап 4.6 (аудит P18): без базового периода рост не измерить — «неизвестно», а не уверенный рост
    if total_recent == 0 or total_base < min_base_total:
        return []
    blocked = {phrase_key(" ".join(item.lower().split())) for item in exclude if item.strip()}
    smoothing = 1.0 / max(1, total_recent + total_base)
    scored: list[EmergingTerm] = []
    for key, count in recent.items():
        if count < min_recent or count / total_recent > max_share or key in blocked:
            continue
        if any(key in other or other in key for other in blocked if " " in other):
            continue
        rate_recent = count / total_recent
        rate_base = base[key] / total_base if total_base else 0.0
        growth = (rate_recent + smoothing) / (rate_base + smoothing)
        if growth < min_growth:
            continue  # этап 4.6 (P18): стабильные и падающие термины — не возникновение
        scored.append(EmergingTerm(surfaces[key].most_common(1)[0][0], count, base[key],
                                   round(growth * math.log2(1 + count), 4)))
    scored.sort(key=lambda item: (-item.score, -len(item.term.split()), item.term))
    chosen: list[EmergingTerm] = []
    for item in scored:
        if any(item.term in other.term and other.recent >= item.recent for other in chosen):
            continue  # вложенный вариант не чаще длинного — поглощён
        chosen = [other for other in chosen if not (other.term in item.term and other.recent <= item.recent)]
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen
