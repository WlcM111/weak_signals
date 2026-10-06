"""Бюджет запросов и паузы по источникам (аудит B2-b, этап 3).

Стенд 04.10.2026: Semantic Scholar в 4 темах из 6 отдал первый же ответ 429 и выбыл из сбора целиком — адаптер не
повторял запрос после паузы. Политика источника задаёт:
- `max_requests` — сколько логических запросов источник может сделать за один сбор (0 — без ограничения);
- `rate_limit_retries` — сколько раз повторить запрос после ответа 429 (пауза — у лимитера источника);
- `rate_limit_pause` — пауза после 429, если источник не прислал `Retry-After` (0 — пауза лимитера по умолчанию).

Для источников без явной политики поведение прежнее: без бюджета и без повторов после 429.
"""

from __future__ import annotations

from dataclasses import dataclass

from collector.domain.values import SourceKey


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Бюджет и паузы одного источника на один сбор."""

    max_requests: int = 0
    rate_limit_retries: int = 0
    rate_limit_pause: float = 0.0

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.rate_limit_retries < 0 or self.rate_limit_pause < 0:
            raise ValueError("параметры политики источника не могут быть отрицательными")
        if self.rate_limit_retries > 5 or self.rate_limit_pause > 120:
            raise ValueError("rate_limit_retries ≤ 5, rate_limit_pause ≤ 120 с")


# Semantic Scholar: bulk-поиск отдаёт до 1000 записей за запрос, шести хватает на три фразы с запасом; анонимный
# пул отвечает 429 без Retry-After — пауза 10 с и два повтора. GDELT: не больше 8 окон истории за сбор (правило GDELT
# «не чаще раза в 5 с» и ответы 11–16 с не дают сделать больше за 120 с бюджета сбора); паузы — у шлюза адаптера.
DEFAULT_POLICIES: dict[SourceKey, SourcePolicy] = {
    SourceKey.SEMANTIC_SCHOLAR: SourcePolicy(max_requests=6, rate_limit_retries=2, rate_limit_pause=10.0),
    SourceKey.GDELT: SourcePolicy(max_requests=8),
}

_FIELDS = {"requests": "max_requests", "retries": "rate_limit_retries", "pause": "rate_limit_pause"}


def parse_source_policies(text: str) -> dict[SourceKey, SourcePolicy]:
    """`источник:requests=6,retries=2,pause=10;gdelt:requests=8` → политики поверх значений по умолчанию."""
    policies = dict(DEFAULT_POLICIES)
    for part in (item.strip() for item in text.split(";")):
        if not part:
            continue
        name, _, body = part.partition(":")
        try:
            key = SourceKey(name.strip())
        except ValueError as exc:
            raise ValueError(f"неизвестный источник в политике: {name.strip()!r}") from exc
        values: dict[str, float] = {}
        for pair in (item.strip() for item in body.split(",")):
            if not pair:
                continue
            field, _, raw = pair.partition("=")
            if field.strip() not in _FIELDS or not raw.strip():
                raise ValueError(f"политика {name.strip()}: ожидается requests=, retries= или pause=, получено {pair!r}")
            values[_FIELDS[field.strip()]] = float(raw)
        base = policies.get(key, SourcePolicy())
        policies[key] = SourcePolicy(
            max_requests=int(values.get("max_requests", base.max_requests)),
            rate_limit_retries=int(values.get("rate_limit_retries", base.rate_limit_retries)),
            rate_limit_pause=float(values.get("rate_limit_pause", base.rate_limit_pause)),
        )
    return policies
