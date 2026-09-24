"""Адаптер OpenAlex: научные публикации (`/works`).

Ключ передаётся параметром `api_key` (§24 HANDOFF: условия ключа не перепроверены — при изменении
документации меняется только этот модуль). Аннотация восстанавливается из `abstract_inverted_index`.
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

API_URL = "https://api.openalex.org/works"
SELECT_FIELDS = "id,doi,title,display_name,abstract_inverted_index,publication_date,cited_by_count,language,primary_location"
MAX_PAGES = 10
# arXiv (Cornell University) как источник в OpenAlex: 3,2 млн работ; найдено запросом
# /sources?search=arxiv 23.09.2026. Препринты arXiv берутся через OpenAlex, не нагружая API arXiv.
ARXIV_SOURCE_ID = "S4306400194"


class OpenAlexAdapter(BaseSourceAdapter):
    """Поиск публикаций OpenAlex по фразам запроса."""

    key = SourceKey.OPENALEX
    raw_meta_keys = frozenset({"concepts", "type"})
    allowed_hosts = frozenset({"api.openalex.org"})

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, api_key: str) -> None:
        super().__init__(http, limiter, clock)
        if not api_key:
            raise ValueError("OpenAlexAdapter требует WS_OPENALEX_API_KEY")
        self._api_key = api_key

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Документы по всем фразам: лимит источника делится поровну между запросами.

        Раньше фразы обходились по очереди до исчерпания лимита, а русские шли первыми: первая русская
        фраза за три страницы забирала все 150 документов, и английские фразы не запрашивались вовсе.
        Теперь каждая английская фраза даёт два запроса — препринты arXiv и вся база, — а русские
        фразы идут после английских; каждый запрос получает свою долю лимита.
        """
        queries = _query_plan(terms, limits.published_since_year)
        if not queries:
            return
        quota = -(-limits.max_documents_per_source // len(queries))
        per_page = min(PAGE_SIZE, quota)
        produced = 0
        for term, filter_value in queries:
            taken = 0
            for page in range(1, MAX_PAGES + 1):
                if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                    return
                if taken >= quota:
                    break
                payload = await self.fetch_json(
                    API_URL,
                    params={
                        "search": term,
                        "filter": filter_value,
                        "per-page": per_page,
                        "page": page,
                        "select": SELECT_FIELDS,
                        "api_key": self._api_key,
                    },
                )
                results = _results(payload)
                for item in results:
                    document = _to_raw_document(item, term)
                    if document is None:
                        continue
                    produced += 1
                    taken += 1
                    yield document
                    if produced >= limits.max_documents_per_source or taken >= quota:
                        break
                if len(results) < per_page:
                    break


def _query_plan(terms: SearchTerms, since_year: int) -> list[tuple[str, str]]:
    """Запросы в порядке ценности: препринты arXiv, вся база по английским фразам, затем русские."""
    year = f"publication_year:>{since_year - 1}"
    pairs = terms.pairs()
    english = [term for term, language in pairs if language == "en"]
    other = [term for term, language in pairs if language != "en"]
    return (
        [(term, f"primary_location.source.id:{ARXIV_SOURCE_ID},{year}") for term in english]
        + [(term, year) for term in english]
        + [(term, year) for term in other]
    )


def _results(payload: Any) -> list[dict[str, Any]]:
    """Извлекает список работ; структурно неверный ответ → PARSE_ERROR."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "OpenAlex: в ответе нет списка results")
    return [item for item in payload["results"] if isinstance(item, dict)]


def _to_raw_document(item: dict[str, Any], term: str) -> RawDocument | None:
    """Преобразует работу OpenAlex в сырой документ; None, если нет URL или заголовка."""
    title = item.get("title") or item.get("display_name") or ""
    url = _landing_url(item)
    if not title or not url:
        return None
    citations = item.get("cited_by_count")
    return RawDocument(
        url=url,
        title=str(title),
        text=restore_abstract(item.get("abstract_inverted_index")),
        matched_term=term,
        published_at=parse_datetime(item.get("publication_date")),
        language_code=item.get("language") if isinstance(item.get("language"), str) else None,
        doi=item.get("doi") if isinstance(item.get("doi"), str) else None,
        citation_count=int(citations) if isinstance(citations, int) else None,
        raw_meta={
            "concepts": [
                concept.get("display_name", "")
                for concept in item.get("concepts", [])[:10]
                if isinstance(concept, dict)
            ],
            "type": item.get("type", ""),
        },
    )


def _landing_url(item: dict[str, Any]) -> str:
    """Ссылка на публикацию: landing page первичного размещения, иначе идентификатор OpenAlex."""
    location = item.get("primary_location")
    if isinstance(location, dict):
        landing = location.get("landing_page_url")
        if isinstance(landing, str) and landing.startswith(("http://", "https://")):
            return landing
    identifier = item.get("id")
    return identifier if isinstance(identifier, str) and identifier.startswith("http") else ""


def restore_abstract(inverted_index: Any) -> str:
    """Восстанавливает аннотацию из инвертированного индекса `{слово: [позиции]}`."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue
        positions.extend((int(index), str(word)) for index in indexes if isinstance(index, int))
    return " ".join(word for _, word in sorted(positions))
