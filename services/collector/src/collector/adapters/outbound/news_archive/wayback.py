"""Перечисление статей изданий через Wayback Machine CDX API (этап 4.3).

Запрос: `web.archive.org/cdx/search/cdx?url=<префикс>&matchType=prefix&output=json&fl=original,timestamp`
`&collapse=urlkey&filter=statuscode:200&filter=mimetype:text/html&limit=N&showResumeKey=true`, для изданий без
даты в адресе — ещё `from`/`to` (месяц сохранения). Проверка из сети стенда 10.10.2026: поиск по префиксу
`techcrunch.com/2026/` — 200 без авторизации; копии есть у 9 из 12 проверенных новостных статей.
Хранится адрес, дата (из адреса, иначе дата сохранения) и слова адреса — без загрузки самих страниц.
Пауза между запросами — `pause_seconds` (по умолчанию 3 с); 429/5xx — повтор после Retry-After, не больше 3 раз.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import yaml

from collector.application.ports import HttpClient, HttpStatusError, HttpTimeoutError, HttpTransportError, NewsArchiveStore
from collector.domain.archive import ArchiveEntry
from collector.domain.text_tokens import slug_title, tokens_of
from ws_common.clock import Clock

CDX_URL = "https://web.archive.org/cdx/search/cdx"
CDX_HOSTS = frozenset({"web.archive.org"})
PAGE_LIMIT = 20000
MAX_PAGES = 4
MAX_TRIES = 3
EXCLUDED_SEGMENTS = frozenset("""tag tags category categories author authors page feed wp-content wp-json wp-admin amp
search events event video videos podcast podcasts newsletter newsletters subscribe about contact privacy terms login
sponsored advertise jobs careers topic topics series people company companies""".split())
TECH_WORDS = ("ai", "robot", "robots", "robotics", "chip", "chips", "semiconductor", "datacenter", "data-center",
              "quantum", "fintech", "payment", "payments", "stablecoin", "blockchain", "crypto", "token", "tokenized",
              "cyber", "cybersecurity", "security", "edge", "cloud", "sensor", "sensors", "autonomous", "drone",
              "drones", "battery", "photonic", "photonics", "neuromorphic", "lidar", "gpu", "gpus", "llm", "agent",
              "agents", "agentic", "inference", "software", "startup", "funding", "raises", "machine-learning")
TECH_FILTER = r"original:(?i).*[/_-](" + "|".join(re.escape(w) for w in TECH_WORDS) + r")([/_.-].*|$)"
_DATE_PATTERNS = (
    re.compile(r"/(20\d\d)/(\d\d)/(\d\d)(?:/|$)"),
    re.compile(r"-(20\d\d)-(\d\d)-(\d\d)/?$"),
    re.compile(r"/news/home/(20\d\d)(\d\d)(\d\d)\d+/"),
    re.compile(r"/(20\d\d)/(\d\d)(?:/|$)"),
)


@dataclass(frozen=True)
class Publisher:
    host: str
    mode: str  # "url_month" | "capture_month"
    prefixes: tuple[str, ...]
    tech_filter: bool = False


def load_publishers(path: Path) -> list[Publisher]:
    """Издания из `news_publishers.yaml`; неверный режим или пустые префиксы — ValueError."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    publishers = []
    for item in data.get("publishers") or []:
        mode = item.get("mode")
        prefixes = tuple(item.get("prefixes") or ())
        if mode not in ("url_month", "capture_month") or not prefixes or not item.get("host"):
            raise ValueError(f"news_publishers: неверная запись {item!r}")
        publishers.append(Publisher(str(item["host"]).lower(), mode, prefixes, bool(item.get("tech_filter"))))
    return publishers


def months_between(first: str, last: str) -> list[str]:
    """Месяцы «ГГГГ-ММ» от `first` до `last` включительно."""
    year, month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def cdx_query(prefix: str, period: str, publisher: Publisher, resume_key: str | None) -> str:
    """Адрес запроса CDX для префикса и месяца; повторяющиеся `filter` — поэтому строка запроса собирается здесь."""
    year, month = period.split("-")
    params: list[tuple[str, str]] = [
        ("url", prefix.format(yyyy=year, mm=month)), ("matchType", "prefix"), ("output", "json"),
        ("fl", "original,timestamp"), ("collapse", "urlkey"), ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"), ("limit", str(PAGE_LIMIT)), ("showResumeKey", "true"),
    ]
    if publisher.mode == "capture_month":
        params += [("from", f"{year}{month}"), ("to", f"{year}{month}")]
    if publisher.tech_filter:
        params.append(("filter", TECH_FILTER))
    if resume_key:
        params.append(("resumeKey", resume_key))
    return f"{CDX_URL}?{urlencode(params)}"


def parse_cdx(body: str) -> tuple[list[tuple[str, str]], str | None]:
    """Строки CDX (`original`, `timestamp`) и ключ продолжения; первая строка — заголовок полей."""
    text = (body or "").strip()
    if not text:
        return [], None
    data = json.loads(text)
    rows: list[tuple[str, str]] = []
    resume_key = None
    seen_blank = False
    for row in data[1:] if data and data[0] and data[0][0] == "original" else data:
        if not row:
            seen_blank = True
            continue
        if seen_blank:
            resume_key = str(row[0])
            continue
        if len(row) >= 2:
            rows.append((str(row[0]), str(row[1])))
    return rows, resume_key


def published_from_path(path: str) -> date | None:
    """Дата публикации из адреса статьи, если она там есть."""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(path)
        if match:
            parts = [int(value) for value in match.groups()] + [1] * (3 - len(match.groups()))
            try:
                return date(parts[0], parts[1], parts[2])
            except ValueError:
                return None
    return None


def article_entry(original: str, timestamp: str, publisher: str) -> ArchiveEntry | None:
    """Адрес из CDX → запись архива; не статья (раздел, тег, автор, файл) → None."""
    parts = urlsplit(original.strip())
    host = (parts.hostname or "").lower()
    if not (host == publisher or host.endswith("." + publisher)):
        return None
    path = parts.path or "/"
    if any(segment in EXCLUDED_SEGMENTS for segment in path.lower().split("/")):
        return None
    if re.search(r"\.(jpe?g|png|gif|webp|svg|pdf|xml|json|css|js|zip|mp[34])$", path, re.IGNORECASE):
        return None
    title = slug_title(path)
    words = [word for word in title.split() if re.search(r"[a-z]", word, re.IGNORECASE)]
    if len(words) < 3:
        return None
    try:
        captured = datetime.strptime(timestamp[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        captured = None
    published = published_from_path(path)  # этап 4.6 (P09): дата сохранения в архиве — не дата публикации
    tokens = tokens_of(title)
    if not tokens:
        return None
    return ArchiveEntry(url=f"https://{host}{path}", publisher=publisher, origin="wayback",
                        title=title[:1].upper() + title[1:], published_on=published, captured_at=captured,
                        tokens=tuple(tokens), published_source="url" if published else "capture", text_status="slug")


class WaybackHarvester:
    """Перечисляет статьи изданий по месяцам и сохраняет их в архив; состояние — по изданию и месяцу."""

    def __init__(self, http: HttpClient, store: NewsArchiveStore, clock: Clock, pause_seconds: float = 3.0,
                 report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._clock = http, store, clock
        self._pause = pause_seconds
        self._report = report

    async def harvest(self, publishers: Sequence[Publisher], months: Sequence[str], refresh: Sequence[str] = ()) -> int:
        """Все месяцы всех изданий; перечисленные до конца пропускаются, кроме месяцев `refresh`.

        Этап 4.6 (аудит P11): месяц, где после MAX_PAGES страниц осталось продолжение, получает статус partial и
        место продолжения; следующий запуск продолжает с него, а done ставится только по подтверждённому концу.
        """
        total = 0
        for publisher in publishers:
            states = await self._store.harvest_states(publisher.host)
            for period in months:
                status, resume = states.get(period, ("", None))
                if status == "done" and period not in refresh:
                    continue
                saved, detail, left = await self._harvest_month(publisher, period, resume if status == "partial" else None)
                state = "failed" if detail is not None else ("partial" if left else "done")
                await self._store.mark_harvest(publisher.host, period, state, saved, detail or "", left)
                total += saved
                note = {"done": f"записей {saved}", "partial": f"записей {saved}, продолжение — следующим запуском",
                        "failed": f"ОТКАЗ {detail}"}[state]
                self._report(f"{publisher.host:30} {period}: {note}")
        return total

    async def _harvest_month(self, publisher: Publisher, period: str,
                             resume: str | None = None) -> tuple[int, str | None, str | None]:
        """Записей сохранено, причина отказа и место продолжения («номер префикса|ключ») либо None."""
        saved = 0
        start, resume_key = 0, None
        if resume and "|" in resume:
            index, _, key = resume.partition("|")
            start, resume_key = (int(index), key or None) if index.isdigit() else (0, None)
        for index, prefix in enumerate(publisher.prefixes):
            if index < start:
                continue
            key = resume_key if index == start else None
            for _page in range(MAX_PAGES):
                body, failure = await self._fetch(cdx_query(prefix, period, publisher, key))
                if failure is not None:
                    return saved, failure, None
                try:
                    rows, key = parse_cdx(body)
                except ValueError as error:
                    return saved, f"ответ CDX не JSON: {error}", None
                entries = [entry for entry in (article_entry(o, t, publisher.host) for o, t in rows) if entry]
                saved += await self._store.save_entries(entries)
                if not key:
                    break
            if key:
                return saved, None, f"{index}|{key}"
        return saved, None, None

    async def _fetch(self, url: str) -> tuple[str, str | None]:
        """Запрос с паузой перед ним; 429/5xx/таймаут — повтор после Retry-After (или 30 с), не больше 3 попыток."""
        detail = "нет ответа"
        for attempt in range(MAX_TRIES):
            await self._clock.sleep(self._pause)
            try:
                return await self._http.get_text(url, allowed_hosts=CDX_HOSTS), None
            except HttpStatusError as error:
                detail = f"код {error.status}"
                if error.status != 429 and error.status < 500:
                    return "", detail
                await self._clock.sleep(error.retry_after or 30.0 * (attempt + 1))
            except (HttpTimeoutError, HttpTransportError) as error:
                detail = f"{type(error).__name__}: {error}"
                await self._clock.sleep(30.0 * (attempt + 1))
        return "", detail
