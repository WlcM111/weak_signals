"""Адаптер Semantic Scholar: научные публикации и препринты через Graph API, bulk-поиск (`/paper/search/bulk`).

Этап 3 (аудит B3, раздел 10.3): вместо поиска по релевантности с двумя страницами по 50 — bulk-поиск с сортировкой
`publicationDate:desc` в окне `window_months` (по умолчанию 24 месяца). Bulk отдаёт до 1000 записей за запрос
и продолжение по `token`. Запрос — все слова фразы без кавычек: bulk по умолчанию требует присутствия всех слов
в любом порядке. Точная фраза в кавычках (этап 3) почти ничего не находила для длинных фраз — прогон 06.10.2026:
«инфраструктура для ИИ» 97 → 1 документ, «робототехника» 0 (этап 3.1). Служебные символы синтаксиса bulk-запроса
(`+ | - " * ( ) ~`) убираются. Квота документов источника делится между фразами поровну.

Semantic Scholar индексирует журналы, конференции и препринты, включая arXiv, поэтому источник
страхует адаптер arXiv, который ограничивает частоту на пограничном прокси. Ключ необязателен:
с ним лимит 1 запрос в секунду, без него — общий пул анонимных клиентов, который при нагрузке
урезается. Ключ передаётся заголовком `x-api-key` и в логи не попадает.
"""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from collector.adapters.outbound.sources.base import BaseSourceAdapter, parse_datetime
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
# Этап 4: прицельный поиск — обычный поиск с ранжированием по релевантности (bulk ранжирования не имеет).
RELEVANCE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SORT = "publicationDate:desc"
DEFAULT_WINDOW_MONTHS = 24
DAYS_PER_MONTH = 30.44
MAX_TERMS = 3
_SYNTAX_RE = re.compile(r'[+|\-"*()~]')
FIELDS = "title,abstract,url,year,publicationDate,externalIds,citationCount,venue,publicationTypes"
PAPER_URL = "https://www.semanticscholar.org/paper/"


class SemanticScholarAdapter(BaseSourceAdapter):
    """Поиск публикаций Semantic Scholar по англоязычным фразам запроса."""

    key = SourceKey.SEMANTIC_SCHOLAR
    raw_meta_keys = frozenset({"venue", "publication_types", "arxiv_id"})
    allowed_hosts = frozenset({"api.semanticscholar.org"})

    def __init__(
        self, http: HttpClient, limiter: RateLimiter, clock: Clock, api_key: str = "",
        window_months: int = DEFAULT_WINDOW_MONTHS,
    ) -> None:
        super().__init__(http, limiter, clock)
        self._headers = {"x-api-key": api_key} if api_key else {}
        self._window_months = window_months

    supports_targeted = True

    async def search_targeted(
        self, technologies: Sequence[str], per_term: int, since: date, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Этап 4: технология — поиск по релевантности, публикации не раньше `since`."""
        for term in technologies:
            query = _relevance_query(term)
            if not query or self.deadline_reached(deadline):
                continue
            payload = await self.fetch_json(RELEVANCE_URL, params={
                "query": query, "fields": FIELDS, "limit": per_term, "publicationDateOrYear": f"{since.isoformat()}:",
            }, headers=self._headers)
            taken = 0
            for paper in _papers(payload):
                document = _to_raw_document(paper, term)
                if document is None:
                    continue
                taken += 1
                yield document
                if taken >= per_term:
                    break

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Свежие публикации по англоязычным фразам: bulk-поиск, новые первыми, квота поровну на фразу."""
        phrases = [term for term in (list(terms.en) or [term for term, _language in terms.pairs()]) if bulk_query(term)]
        phrases = phrases[:MAX_TERMS]
        if not phrases:
            return
        quota = math.ceil(limits.max_documents_per_source / len(phrases))
        since = window_start(self._clock.now(), self._window_months, limits.published_since_year)
        produced = 0
        for term in phrases:
            taken = 0
            token: str | None = None
            while taken < quota and produced < limits.max_documents_per_source and not self.deadline_reached(deadline):
                params: dict[str, str | int] = {
                    "query": bulk_query(term),
                    "fields": FIELDS,
                    "sort": SORT,
                    "publicationDateOrYear": f"{since.isoformat()}:",
                }
                if token:
                    params["token"] = token
                payload = await self.fetch_json(API_URL, params=params, headers=self._headers)
                papers = _papers(payload)
                for paper in papers:
                    document = _to_raw_document(paper, term)
                    if document is None:
                        continue
                    taken += 1
                    produced += 1
                    yield document
                    if taken >= quota or produced >= limits.max_documents_per_source:
                        break
                next_token = payload.get("token") if isinstance(payload, dict) else None
                token = next_token if isinstance(next_token, str) and next_token else None
                if token is None or not papers:
                    break


def _relevance_query(term: str) -> str:
    return " ".join(_SYNTAX_RE.sub(" ", term).split())


def bulk_query(term: str) -> str:
    """Запрос bulk-поиска: слова фразы без служебных символов синтаксиса — все слова обязательны, порядок любой."""
    return " ".join(_SYNTAX_RE.sub(" ", term).split())


def window_start(now: datetime, months: int, since_year: int) -> datetime.date:  # type: ignore[valid-type]
    """Начало окна публикаций: `months` месяцев назад, но не раньше 1 января `since_year`."""
    start = (now.astimezone(UTC) - timedelta(days=round(months * DAYS_PER_MONTH))).date()
    if since_year:
        start = max(start, datetime(since_year, 1, 1, tzinfo=UTC).date())
    return start


def _papers(payload: Any) -> list[dict[str, Any]]:
    """Список статей из ответа; структурно неверный ответ → PARSE_ERROR."""
    if not isinstance(payload, dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Semantic Scholar: ответ не объект JSON")
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Semantic Scholar: поле data не список")
    return [paper for paper in data if isinstance(paper, dict)]


def _to_raw_document(paper: dict[str, Any], term: str) -> RawDocument | None:
    """Статья Semantic Scholar → сырой документ; None без заголовка или ссылки."""
    title = paper.get("title")
    url = _paper_url(paper)
    if not isinstance(title, str) or not title.strip() or not url:
        return None
    external = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    doi = external.get("DOI")
    citations = paper.get("citationCount")
    published = paper.get("publicationDate") or (f"{paper['year']}-01-01" if isinstance(paper.get("year"), int) else None)
    abstract = paper.get("abstract")
    return RawDocument(
        url=url,
        title=title.strip(),
        text=abstract if isinstance(abstract, str) else "",
        matched_term=term,
        published_at=parse_datetime(published),
        language_code=None,
        doi=f"https://doi.org/{doi}" if isinstance(doi, str) and doi else None,
        citation_count=citations if isinstance(citations, int) else None,
        raw_meta={
            "venue": paper.get("venue") if isinstance(paper.get("venue"), str) else "",
            "publication_types": [t for t in paper.get("publicationTypes") or [] if isinstance(t, str)][:5],
            "arxiv_id": external.get("ArXiv") if isinstance(external.get("ArXiv"), str) else "",
        },
    )


def _paper_url(paper: dict[str, Any]) -> str:
    """Ссылка на статью: собственная страница Semantic Scholar, иначе по идентификатору."""
    url = paper.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    paper_id = paper.get("paperId")
    return f"{PAPER_URL}{paper_id}" if isinstance(paper_id, str) and paper_id else ""