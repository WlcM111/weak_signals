"""Бюджет запросов и паузы по источникам (аудит B2-b, этап 3).

Стенд 04.10.2026: Semantic Scholar в 4 темах из 6 отдал первый же ответ 429 и выбыл из сбора целиком — адаптер не
повторял запрос после паузы. Политика источника задаёт:
- `max_requests` — сколько логических запросов источник может сделать за один сбор (0 — без ограничения);
- `rate_limit_retries` — сколько раз повторить запрос после ответа 429 (пауза — у лимитера источника);
- `rate_limit_pause` — пауза после 429, если источник не прислал `Retry-After` (0 — пауза лимитера по умолчанию).

Для источников без явной политики поведение прежнее: без бюджета и без повторов после 429.
"""

from __future__ import annotations

from collections.abc import Mapping

from dataclasses import dataclass

from collector.domain.values import SourceKey


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Бюджет и паузы одного источника на один сбор."""

    max_requests: int = 0
    rate_limit_retries: int = 0
    rate_limit_pause: float = 0.0
    # Этап 4.2: повтор после сетевого сбоя, таймаута или 5xx. Повторы делает только адаптер (через лимитер);
    # HTTP-клиент источников больше не повторяет сам (аудит API 07.10.2026, п. 2.1).
    transient_retries: int = 1

    def __post_init__(self) -> None:
        if min(self.max_requests, self.rate_limit_retries, self.transient_retries) < 0 or self.rate_limit_pause < 0:
            raise ValueError("параметры политики источника не могут быть отрицательными")
        if self.rate_limit_retries > 5 or self.rate_limit_pause > 120 or self.transient_retries > 3:
            raise ValueError("rate_limit_retries ≤ 5, rate_limit_pause ≤ 120 с, transient_retries ≤ 3")


# Semantic Scholar: bulk-поиск отдаёт до 1000 записей за запрос, шести хватает на три фразы с запасом; анонимный
# пул отвечает 429 без Retry-After — пауза 10 с и два повтора. GDELT: не больше 8 окон истории за сбор (правило GDELT
# «не чаще раза в 5 с» и ответы 11–16 с не дают сделать больше за 120 с бюджета сбора); паузы — у шлюза адаптера.
DEFAULT_POLICIES: dict[SourceKey, SourcePolicy] = {
    # arXiv: собственный шлюз и пауза после отказа; быстрых повторов нет (этап 4.2).
    SourceKey.ARXIV: SourcePolicy(transient_retries=0),
    SourceKey.ZENODO: SourcePolicy(transient_retries=0),  # шлюз Zenodo сам держит интервал и паузу
    # этап 4.3: новые источники — до 2 фраз широкого прохода и до 40 технологий прицельного
    SourceKey.CROSSREF: SourcePolicy(max_requests=60, rate_limit_retries=1, rate_limit_pause=10.0),
    # этап 4.7: бюджет ключа OpenAlex — $1 в сутки = 1000 поисковых запросов; 40 на сбор → 240 на замер из 6 тем
    SourceKey.OPENALEX: SourcePolicy(max_requests=40),
    SourceKey.HUGGINGFACE: SourcePolicy(max_requests=60, rate_limit_retries=1, rate_limit_pause=10.0),
    SourceKey.NEWS_ARCHIVE: SourcePolicy(transient_retries=0),  # локальный архив, внешних запросов нет
    # этап 4: + до 40 прицельных запросов по технологиям
    SourceKey.SEMANTIC_SCHOLAR: SourcePolicy(max_requests=50, rate_limit_retries=2, rate_limit_pause=10.0),
    SourceKey.GDELT: SourcePolicy(max_requests=8, transient_retries=0),  # повтор окна — в самом адаптере
    # Hacker News: две фразы × девять окон за 24 месяца; ответ ~0,6 с, лимит 10 000 запросов в час с IP.
    # этап 4.5: 18 широкого прохода + 40 технологий + второй круг — 60 не хватало (BUDGET_EXHAUSTED во всех темах)
    SourceKey.HACKERNEWS: SourcePolicy(max_requests=80, rate_limit_retries=1, rate_limit_pause=10.0),
}

_FIELDS = {"requests": "max_requests", "retries": "rate_limit_retries", "pause": "rate_limit_pause", "transient": "transient_retries"}


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
                raise ValueError(f"политика {name.strip()}: ожидается requests=, retries=, pause= или transient=, получено {pair!r}")
            values[_FIELDS[field.strip()]] = float(raw)
        base = policies.get(key, SourcePolicy())
        policies[key] = SourcePolicy(
            max_requests=int(values.get("max_requests", base.max_requests)),
            rate_limit_retries=int(values.get("rate_limit_retries", base.rate_limit_retries)),
            rate_limit_pause=float(values.get("rate_limit_pause", base.rate_limit_pause)),
            transient_retries=int(values.get("transient_retries", base.transient_retries)),
        )
    return policies


# Этап 4.2: верхние границы частоты по опубликованным правилам сервисов (аудит API 07.10.2026). Каталог БД может
# задать меньше — берётся меньшее. Без всплесков: лимитер выдаёт запросы равномерно (ёмкость 1).
ARXIV_MAX_RPS = 0.3            # не чаще 1 запроса в 3 с — условия arXiv API; запас до 3,3 с
GITHUB_SEARCH_ANON_RPS = 1 / 7  # поиск без токена: 10 в минуту → 1 запрос в 7 с
GITHUB_SEARCH_TOKEN_RPS = 0.45  # поиск с токеном: 30 в минуту → 27 в минуту с запасом
SEMANTIC_SCHOLAR_KEY_RPS = 0.8  # ключ: 1 запрос/с суммарно по endpoint → раз в 1,25 с
SEMANTIC_SCHOLAR_ANON_RPS = 0.5
WIKIPEDIA_MAX_RPS = 2.0        # анонимный бот с контактом в User-Agent: 200 в минуту
ZENODO_SEARCH_MAX_RPS = 0.4    # поиск Records: 30 в минуту


def effective_rates(
    catalog: Mapping[SourceKey, float], *, github_token: bool, semantic_scholar_key: bool
) -> dict[SourceKey, float]:
    """Частоты лимитера: значение каталога, но не выше опубликованного правила сервиса для текущего режима доступа."""
    caps = {
        SourceKey.ARXIV: ARXIV_MAX_RPS,
        SourceKey.GITHUB: GITHUB_SEARCH_TOKEN_RPS if github_token else GITHUB_SEARCH_ANON_RPS,
        SourceKey.SEMANTIC_SCHOLAR: SEMANTIC_SCHOLAR_KEY_RPS if semantic_scholar_key else SEMANTIC_SCHOLAR_ANON_RPS,
        SourceKey.WIKIPEDIA: WIKIPEDIA_MAX_RPS,
        SourceKey.ZENODO: ZENODO_SEARCH_MAX_RPS,
    }
    return {key: min(rate, caps[key]) if key in caps else rate for key, rate in catalog.items()}
