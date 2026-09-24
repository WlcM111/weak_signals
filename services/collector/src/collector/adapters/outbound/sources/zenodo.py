"""Адаптер Zenodo: препринты через REST API поиска записей (`GET /api/records`).

Zenodo — открытый репозиторий CERN и OpenAIRE, метаданные распространяются по CC0. Поиск Zenodo
ограничен 30 запросами в минуту и 25 записями на страницу для анонимных клиентов. Адаптер запрашивает
только препринты (`type=publication`, `subtype=preprint` — значения чувствительны к регистру: вариант
`subtype=Preprint` сервер молча игнорирует, проверено 2026-09-22), свежие сверху (`sort=mostrecent`),
все значимые слова фразы через AND (точная фраза в кавычках почти всегда даёт пустой ответ),
и страхуется от сбоев сервиса, который периодически перегружен автоматизированным трафиком:
  * к Zenodo идёт ровно один запрос одновременно на весь процесс, с паузой между запросами
    не меньше MIN_GAP_SECONDS — даже если несколько коллекций собираются параллельно;
  * после отказа, означающего недоступность или ограничение клиента (429, 403, 5xx, таймаут,
    сетевая ошибка), адаптер COOLDOWN_SECONDS не обращается к Zenodo и сразу сообщает причину:
    задание не тратит бюджет времени на заведомо неудачные запросы, а перегруженный сервис
    не получает лишней нагрузки.
Ответ разбирается в двух форматах записей: InvenioRDM (`resource_type.id`, `languages`, `pids`)
и прежнем формате Zenodo (`resource_type.subtype`, `language`, `doi`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from collector.adapters.outbound.sources.base import (
    BaseSourceAdapter,
    english_first,
    parse_datetime,
    within_since_year,
)
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.logging import get_logger

API_URL = "https://zenodo.org/api/records"
RECORD_URL = "https://zenodo.org/records/"
HEADERS = {"Accept": "application/json"}
# Максимум записей на страницу для анонимного поиска Zenodo.
PAGE_SIZE = 25
# Не больше двух фраз и двух страниц на задание: до четырёх запросов к перегруженному сервису.
MAX_TERMS = 2
MAX_PAGES = 2
# Интервал между запросами на весь процесс: 0.3 запроса в секунду, с запасом к лимиту 30 в минуту.
MIN_GAP_SECONDS = 3.4
# Пауза после отказа: окно лимита Zenodo — минута, сбои перемежающиеся, поэтому десяти минут
# достаточно, чтобы переждать ограничение, и не слишком долго, чтобы источник вернулся после сбоя.
COOLDOWN_SECONDS = 10 * 60
# Коды отказов, означающие недоступность Zenodo или ограничение клиента. HTTP_5XX включает
# сетевые ошибки: базовый адаптер переводит их в этот код.
COOLDOWN_CODES = frozenset(
    {AdapterErrorCode.RATE_LIMITED.value, AdapterErrorCode.HTTP_5XX.value, AdapterErrorCode.TIMEOUT.value}
)
# Ответ 403 Zenodo использует для блокировки клиентов, нарушающих лимиты.
COOLDOWN_STATUSES = frozenset({403})
PREPRINT_TYPE_ID = "publication-preprint"
_STATUS_RE = re.compile(r"кодом (\d{3})")
_SPACES_RE = re.compile(r"\s+")
# Служебные символы синтаксиса query string Elasticsearch и слова, не несущие смысла в запросе.
_QUERY_RESERVED_RE = re.compile(r'[+\-=&|><!(){}\[\]^"~*?:\\/]')
_QUERY_STOPWORDS = frozenset(
    {"a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "by", "at", "from", "or", "not", "via"}
)
# ISO 639-2/639-3 → ISO 639-1: коды длиной 3 прошли бы проверку формата, но интерфейс и orchestrator
# сравнивают двухбуквенные коды (например, `language_code == "ru"`).
_ISO_639_1 = {
    "eng": "en",
    "rus": "ru",
    "deu": "de",
    "ger": "de",
    "fra": "fr",
    "fre": "fr",
    "zho": "zh",
    "chi": "zh",
    "jpn": "ja",
    "spa": "es",
    "ita": "it",
    "por": "pt",
}

log = get_logger("collector.zenodo")


class _ZenodoGate:
    """Общий на процесс шлюз к Zenodo: один запрос одновременно, интервал и пауза после отказа."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_request_at: float | None = None
        self.blocked_until = 0.0
        self.blocked_code = ""
        self.blocked_reason = ""

    def reset(self) -> None:
        """Сброс состояния; замок пересоздаётся, чтобы не остаться привязанным к прежнему циклу событий."""
        self.lock = asyncio.Lock()
        self.last_request_at = None
        self.blocked_until = 0.0
        self.blocked_code = ""
        self.blocked_reason = ""


_GATE = _ZenodoGate()


def reset_zenodo_gate() -> None:
    """Сбрасывает интервал и паузу общего шлюза Zenodo (для тестов)."""
    _GATE.reset()


class ZenodoAdapter(BaseSourceAdapter):
    """Поиск препринтов Zenodo по фразам запроса."""

    key = SourceKey.ZENODO
    raw_meta_keys = frozenset({"record_id", "resource_type"})
    allowed_hosts = frozenset({"zenodo.org"})

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Препринты по англоязычным фразам, свежие сверху; фильтр по году публикации."""
        produced = 0
        for term, _language in english_first(terms)[:MAX_TERMS]:
            for page in range(1, MAX_PAGES + 1):
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    return
                payload = await self._fetch_page(
                    {
                        "q": _all_words_query(term),
                        "type": "publication",
                        "subtype": "preprint",
                        "sort": "mostrecent",
                        "size": PAGE_SIZE,
                        "page": page,
                    }
                )
                hits = _hits(payload)
                for hit in hits:
                    document = _to_raw_document(hit, term)
                    if document is None or not within_since_year(
                        document.published_at, limits.published_since_year
                    ):
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
                if len(hits) < PAGE_SIZE:
                    break

    async def _fetch_page(self, params: dict[str, str | int]) -> Any:
        """Один запрос к Zenodo через общий шлюз процесса."""
        async with _GATE.lock:
            now = self._clock.monotonic()
            if now < _GATE.blocked_until:
                raise AdapterFailure(
                    _GATE.blocked_code,
                    f"Zenodo не опрашивается после отказа ({_GATE.blocked_reason}), "
                    f"пауза ещё {int(_GATE.blocked_until - now)} с",
                )
            if _GATE.last_request_at is not None:
                wait = _GATE.last_request_at + MIN_GAP_SECONDS - now
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                return await self.fetch_json(API_URL, params=params, headers=HEADERS)
            except AdapterFailure as failure:
                if _blocks_source(failure):
                    _GATE.blocked_until = self._clock.monotonic() + COOLDOWN_SECONDS
                    _GATE.blocked_code = failure.code
                    _GATE.blocked_reason = failure.message
                    log.warning("zenodo.cooldown", code=failure.code, seconds=COOLDOWN_SECONDS)
                raise
            finally:
                _GATE.last_request_at = self._clock.monotonic()


def _blocks_source(failure: AdapterFailure) -> bool:
    """Означает ли отказ недоступность Zenodo или ограничение клиента."""
    if failure.code in COOLDOWN_CODES:
        return True
    match = _STATUS_RE.search(failure.message or "")
    return match is not None and int(match.group(1)) in COOLDOWN_STATUSES


def _phrase_query(term: str) -> str:
    """Фраза в синтаксисе query string Elasticsearch: в кавычках, без символов, ломающих фразу."""
    cleaned = _SPACES_RE.sub(" ", term.replace("\\", " ").replace('"', " ")).strip()
    return f'"{cleaned}"'


def _all_words_query(term: str) -> str:
    """Все значимые слова фразы через AND: мягче точной фразы, но строже поиска по любому слову."""
    words = [
        word
        for word in _QUERY_RESERVED_RE.sub(" ", term).split()
        if len(word) > 1 and word.lower() not in _QUERY_STOPWORDS
    ]
    return " AND ".join(words) if words else _phrase_query(term)


def _hits(payload: Any) -> list[dict[str, Any]]:
    """Записи из ответа поиска; структурно неверный ответ → PARSE_ERROR."""
    container = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(container, dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Zenodo: в ответе нет объекта hits")
    hits = container.get("hits")
    if not isinstance(hits, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Zenodo: поле hits.hits не список")
    return [hit for hit in hits if isinstance(hit, dict)]


def _to_raw_document(hit: dict[str, Any], term: str) -> RawDocument | None:
    """Запись Zenodo → сырой документ; None для записи не-препринта, без заголовка или ссылки."""
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    if not _is_preprint(metadata):
        return None
    title = metadata.get("title")
    url = _record_url(hit)
    if not isinstance(title, str) or not title.strip() or not url:
        return None
    description = metadata.get("description")
    publication_date = metadata.get("publication_date")
    return RawDocument(
        url=url,
        title=title.strip(),
        text=description if isinstance(description, str) else "",
        matched_term=term,
        published_at=parse_datetime(publication_date if isinstance(publication_date, str) else None),
        language_code=_language(metadata),
        doi=_doi(hit, metadata),
        raw_meta={"record_id": str(hit.get("id") or ""), "resource_type": _resource_type_id(metadata)},
    )


def _is_preprint(metadata: dict[str, Any]) -> bool:
    """Препринт ли запись; при отсутствии типа — да, полагаясь на серверный фильтр subtype=Preprint."""
    resource_type = metadata.get("resource_type")
    if not isinstance(resource_type, dict):
        return True
    identifier = resource_type.get("id")
    if isinstance(identifier, str) and identifier:
        return identifier == PREPRINT_TYPE_ID
    return resource_type.get("subtype") == "preprint"


def _resource_type_id(metadata: dict[str, Any]) -> str:
    """Идентификатор типа записи в форме InvenioRDM (`publication-preprint`)."""
    resource_type = metadata.get("resource_type")
    if not isinstance(resource_type, dict):
        return ""
    identifier = resource_type.get("id")
    if isinstance(identifier, str) and identifier:
        return identifier
    parts = [resource_type.get("type"), resource_type.get("subtype")]
    return "-".join(part for part in parts if isinstance(part, str) and part)


def _record_url(hit: dict[str, Any]) -> str:
    """Ссылка на страницу записи: из links, иначе по идентификатору."""
    links = hit.get("links") if isinstance(hit.get("links"), dict) else {}
    for key in ("self_html", "html"):
        url = links.get(key)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    record_id = hit.get("id")
    return f"{RECORD_URL}{record_id}" if isinstance(record_id, (int, str)) and str(record_id) else ""


def _doi(hit: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """DOI записи из прежнего формата (`doi`) или InvenioRDM (`pids.doi.identifier`)."""
    for candidate in (hit.get("doi"), metadata.get("doi")):
        if isinstance(candidate, str) and candidate:
            return candidate
    pids = hit.get("pids") if isinstance(hit.get("pids"), dict) else {}
    doi = pids.get("doi") if isinstance(pids.get("doi"), dict) else {}
    identifier = doi.get("identifier")
    return identifier if isinstance(identifier, str) and identifier else None


def _language(metadata: dict[str, Any]) -> str | None:
    """Двухбуквенный код языка; None — язык определит нормализация по тексту."""
    code: object = metadata.get("language")
    languages = metadata.get("languages")
    if not isinstance(code, str) and isinstance(languages, list) and languages:
        first = languages[0]
        code = first.get("id") if isinstance(first, dict) else first
    if not isinstance(code, str):
        return None
    normalized = code.strip().lower()
    if normalized in _ISO_639_1:
        return _ISO_639_1[normalized]
    return normalized if len(normalized) == 2 and normalized.isalpha() else None