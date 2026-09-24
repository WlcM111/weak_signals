"""Адаптер Semantic Scholar: научные публикации и препринты через Graph API (`/paper/search`).

Semantic Scholar индексирует журналы, конференции и препринты, включая arXiv, поэтому источник
страхует адаптер arXiv, который ограничивает частоту на пограничном прокси. Ключ необязателен:
с ним лимит 1 запрос в секунду, без него — общий пул анонимных клиентов, который при нагрузке
урезается. Ключ передаётся заголовком `x-api-key` и в логи не попадает.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from collector.adapters.outbound.sources.base import PAGE_SIZE, BaseSourceAdapter, parse_datetime
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,abstract,url,year,publicationDate,externalIds,citationCount,venue,publicationTypes"
PAPER_URL = "https://www.semanticscholar.org/paper/"
# Две страницы на фразу: у Semantic Scholar выдача по релевантности, дальше идёт шум.
MAX_PAGES = 2


class SemanticScholarAdapter(BaseSourceAdapter):
    """Поиск публикаций Semantic Scholar по англоязычным фразам запроса."""

    key = SourceKey.SEMANTIC_SCHOLAR
    raw_meta_keys = frozenset({"venue", "publication_types", "arxiv_id"})
    allowed_hosts = frozenset({"api.semanticscholar.org"})

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, api_key: str = "") -> None:
        super().__init__(http, limiter, clock)
        self._headers = {"x-api-key": api_key} if api_key else {}

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Документы по англоязычным фразам: корпус Semantic Scholar почти целиком англоязычный."""
        produced = 0
        phrases = list(terms.en) or [term for term, _language in terms.pairs()]
        for term in phrases:
            for page in range(MAX_PAGES):
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    return
                payload = await self.fetch_json(
                    API_URL,
                    params={
                        "query": term,
                        "fields": FIELDS,
                        "limit": PAGE_SIZE,
                        "offset": page * PAGE_SIZE,
                        "year": f"{limits.published_since_year}-",
                    },
                    headers=self._headers,
                )
                papers = _papers(payload)
                for paper in papers:
                    document = _to_raw_document(paper, term)
                    if document is None:
                        continue
                    produced += 1
                    yield document
                    if produced >= limits.max_documents_per_source:
                        return
                if len(papers) < PAGE_SIZE:
                    break


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