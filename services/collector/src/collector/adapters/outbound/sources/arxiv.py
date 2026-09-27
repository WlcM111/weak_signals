"""Адаптер arXiv: препринты через Atom API (1 запрос в 3 с, §6.5 ТЗ).

Правила arXiv для этого API: не чаще одного запроса в три секунды и одно соединение одновременно.
API arXiv работает за облачной инфраструктурой с собственными квотами и отказывает даже первому
запросу после простоя: curl получает 429 «Rate exceeded» от Google Frontend, клиент httpx — 406
от Varnish с пустым телом или не получает ответа (диагностика 2026-09-22). Поэтому адаптер:
  * пропускает к arXiv ровно один запрос одновременно на весь процесс, с паузой между запросами
    не меньше MIN_GAP_SECONDS — даже если несколько коллекций собираются параллельно;
  * считает 406 таким же отказом по частоте, как 429, и сообщает его кодом RATE_LIMITED;
  * после отказа (429, 406, 403, 5xx, таймаут, сетевая ошибка) COOLDOWN_SECONDS не обращается
    к arXiv и сразу сообщает причину: задание не тратит бюджет времени на заведомо неудачные
    запросы, а ограничение не продлевается каждым новым заданием.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from collector.adapters.outbound.sources.base import (
    PAGE_SIZE,
    BaseSourceAdapter,
    english_first,
    parse_datetime,
    within_since_year,
)
from collector.adapters.outbound.sources.xml_utils import parse_xml, text_of
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.logging import get_logger

API_URL = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
HEADERS = {"Accept": "application/atom+xml, application/xml;q=0.9"}
# Одна страница на фразу и не больше двух фраз на задание: до двух запросов к arXiv.
MAX_PAGES = 1
MAX_TERMS = 2
# Интервал между запросами на весь процесс (правило arXiv — не меньше трёх секунд).
MIN_GAP_SECONDS = 3.5
# Пауза после отказа: ограничение arXiv держится долго и продлевается повторными обращениями.
COOLDOWN_SECONDS = 30 * 60
# Коды отказов, означающие ограничение клиента или недоступность arXiv. HTTP_5XX включает
# сетевые ошибки: базовый адаптер переводит их в этот код.
COOLDOWN_CODES = frozenset(
    {AdapterErrorCode.RATE_LIMITED.value, AdapterErrorCode.HTTP_5XX.value, AdapterErrorCode.TIMEOUT.value}
)
# 403 — блокировка клиента, 406 — отказ по частоте от Varnish arXiv.
COOLDOWN_STATUSES = frozenset({403, 406})
RATE_LIMIT_STATUSES = frozenset({406})
_STATUS_RE = re.compile(r"кодом (\d{3})")

log = get_logger("collector.arxiv")


class _ArxivGate:
    """Общий на процесс шлюз к arXiv: один запрос одновременно, интервал и пауза после отказа."""

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


_GATE = _ArxivGate()


def reset_arxiv_gate() -> None:
    """Сбрасывает интервал и паузу общего шлюза arXiv (для тестов)."""
    _GATE.reset()


class ArxivAdapter(BaseSourceAdapter):
    """Поиск препринтов arXiv по фразам запроса."""

    key = SourceKey.ARXIV
    raw_meta_keys = frozenset({"categories"})
    allowed_hosts = frozenset({"export.arxiv.org", "arxiv.org"})

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Документы по англоязычным фразам, свежие сверху; фильтр по году публикации."""
        produced = 0
        for term, _language in english_first(terms)[:MAX_TERMS]:
            for page in range(MAX_PAGES):
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    return
                body = await self._fetch_page(
                    {
                        "search_query": search_query(term),
                        "start": page * PAGE_SIZE,
                        "max_results": PAGE_SIZE,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    }
                )
                entries = parse_xml(body, what="arXiv").findall("atom:entry", NS)
                for entry in entries:
                    document = _to_raw_document(entry, term)
                    if document is None or not within_since_year(
                        document.published_at, limits.published_since_year
                    ):
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
                if len(entries) < PAGE_SIZE:
                    break

    async def _fetch_page(self, params: dict[str, str | int]) -> str:
        """Один запрос к arXiv через общий шлюз процесса."""
        async with _GATE.lock:
            now = self._clock.monotonic()
            if now < _GATE.blocked_until:
                raise AdapterFailure(
                    _GATE.blocked_code,
                    f"arXiv не опрашивается после отказа ({_GATE.blocked_reason}), "
                    f"пауза ещё {int(_GATE.blocked_until - now)} с",
                )
            if _GATE.last_request_at is not None:
                wait = _GATE.last_request_at + MIN_GAP_SECONDS - now
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                return await self.fetch_text(API_URL, params=params, headers=HEADERS)
            except AdapterFailure as original:
                failure = _as_rate_limit(original)
                if _blocks_source(failure):
                    _GATE.blocked_until = self._clock.monotonic() + COOLDOWN_SECONDS
                    _GATE.blocked_code = failure.code
                    _GATE.blocked_reason = failure.message
                    log.warning("arxiv.cooldown", code=failure.code, seconds=COOLDOWN_SECONDS)
                if failure is original:
                    raise
                raise failure from original
            finally:
                _GATE.last_request_at = self._clock.monotonic()


def _status(failure: AdapterFailure) -> int | None:
    """HTTP-код из сообщения отказа базового адаптера («источник ответил кодом N»)."""
    match = _STATUS_RE.search(failure.message or "")
    return int(match.group(1)) if match else None


_VIA_RE = re.compile(r"via=([^;\]]*)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_QUERY_STOP = frozenset({"and", "for", "the", "with", "from", "into", "based", "using", "via", "new", "novel"})


def search_query(term: str) -> str:
    """Запрос arXiv: значимые слова фразы через AND, а не точная фраза. 27.09 точная фраза из двух первых фраз
    расширения дала 0 документов в 5 темах кейса из 6; однословная фраза остаётся как есть."""
    words = [w for w in _WORD_RE.findall(term) if w.lower() not in _QUERY_STOP][:4]
    if len(words) < 2:
        return f'all:"{term}"'
    return " AND ".join(f"all:{word}" for word in words)


def _as_rate_limit(failure: AdapterFailure) -> AdapterFailure:
    """406 от arXiv. Без узла «google» в via запрос отклонил CDN (Fastly) и до сервера arXiv не дошёл: так CDN
    отвечает на TLS-рукопожатие OpenSSL 3.5 (проверено 26.09.2026, docs/collector/DECISIONS.md). Код остаётся
    HTTP_4XX (контракт не меняется), причина — в тексте отказа; пауза источника включается, повторов нет."""
    status = _status(failure)
    via = _VIA_RE.search(failure.message or "")
    if status == 406 and via is not None and "google" not in via.group(1).lower():
        details = (failure.message or "").split("[", 1)[-1].rstrip("]")
        return AdapterFailure(
            AdapterErrorCode.HTTP_4XX.value,
            f"CDN arXiv отклонил TLS-клиент, запрос не дошёл до сервера (406 без узла google в via) [{details}]",
        )
    if status in RATE_LIMIT_STATUSES:
        return AdapterFailure(
            AdapterErrorCode.HTTP_4XX.value,
            f"arXiv ответил кодом {status} (Not Acceptable); причина не установлена — наблюдалась при частых "
            "запросах, источник поставлен на паузу",
        )
    return failure


def _blocks_source(failure: AdapterFailure) -> bool:
    """Означает ли отказ ограничение клиента или недоступность arXiv."""
    return failure.code in COOLDOWN_CODES or _status(failure) in COOLDOWN_STATUSES


def _to_raw_document(entry: Any, term: str) -> RawDocument | None:
    """Преобразует запись Atom arXiv в сырой документ."""
    title = text_of(entry.find("atom:title", NS))
    url = text_of(entry.find("atom:id", NS))
    if not title or not url.startswith("http"):
        return None
    doi_element = entry.find("arxiv:doi", NS)
    categories = [
        category.get("term", "") for category in entry.findall("atom:category", NS) if category.get("term")
    ]
    return RawDocument(
        url=url,
        title=title,
        text=text_of(entry.find("atom:summary", NS)),
        matched_term=term,
        published_at=parse_datetime(text_of(entry.find("atom:published", NS))),
        language_code="en",
        doi=text_of(doi_element) or None,
        raw_meta={"categories": categories[:10]},
    )