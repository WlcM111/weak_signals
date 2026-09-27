"""Адаптер поисковой платформы Роспатента (ИС «Поисковая платформа»): патенты РФ, СНГ и мирового фонда.

По документации Роспатента «ИС „Поисковая платформа“. Описание методов программных интерфейсов Системы
(API)» (2022): адрес API `https://searchplatform.rospatent.gov.ru/patsearch/v0.2/`, поиск — `POST /search`
с параметрами в JSON, доступ — https и JWT-токен (API-ключ) в заголовке `Authorization: Bearer`; ключи
выдаются зарегистрированным пользователям в разделе «Генерирование ключей» интерфейса платформы.

Поиск идёт параметром `qn` — запрос на естественном языке: для него нет синтаксических правил, поэтому
фразы темы не нужно экранировать. Первая русская фраза находит документы российского и СНГ массивов,
первая английская — мирового фонда. Фильтр `date_published` (формат YYYYMMDD) ограничивает выдачу годом
`published_since_year`; группировка по патентным семействам у платформы включена по умолчанию.
Документ — патентная публикация: название (`biblio.<язык>.title`), фрагмент описания (`snippet.description`),
дата публикации, коды МПК, заявитель; ссылка — карточка документа на платформе `/doc/<id>`.
Тип PATENT, доверенность HIGH. Лимит частоты в документации не опубликован: клиент вежлив —
0.5 запроса в секунду (каталог источников). Токен в логи и в raw_meta не попадает.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

from collector.adapters.outbound.sources.base import BaseSourceAdapter, parse_datetime, within_since_year
from collector.application.ports import HttpClient, RateLimiter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey
from ws_common.clock import Clock

API_URL = "https://searchplatform.rospatent.gov.ru/patsearch/v0.2/search"
DOC_URL = "https://searchplatform.rospatent.gov.ru/doc/"
# 10, а не 25 документов на фразу: 26.09 патенты заняли 25 из 31 источника карточек по темам кейса, и карточки
# стали общими («промышленный IoT с ИИ»), а сигналы организаторов — рыночные.
PAGE_SIZE = 10
TERMS_PER_LANGUAGE = 1
MAX_IPC = 5
MAX_NAMES = 3
_DATE_RE = re.compile(r"^(\d{4})[.\-/]?(\d{2})[.\-/]?(\d{2})$")


class RospatentAdapter(BaseSourceAdapter):
    """Поиск патентных документов по русской и английской фразам темы."""

    key = SourceKey.ROSPATENT
    raw_meta_keys = frozenset({"ipc", "applicant", "kind", "publishing_office", "dataset"})
    allowed_hosts = frozenset({"searchplatform.rospatent.gov.ru"})

    def __init__(self, http: HttpClient, limiter: RateLimiter, clock: Clock, token: str) -> None:
        super().__init__(http, limiter, clock)
        if not token.strip():
            raise ValueError("не задан WS_ROSPATENT_TOKEN — API-ключ поисковой платформы Роспатента")
        self._headers = {"Authorization": f"Bearer {token.strip()}"}

    async def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Патенты по одной русской и одной английской фразе; повтор документа между фразами пропускается."""
        produced = 0
        seen: set[str] = set()
        for term, language in query_plan(terms):
            if produced >= limits.max_documents_per_source or self.deadline_reached(deadline):
                return
            payload = await self.fetch_json_post(API_URL, json_body=request_body(term, limits), headers=self._headers)
            for hit in _hits(payload):
                document = _to_raw_document(hit, term, language)
                if document is None or document.url in seen:
                    continue
                if not within_since_year(document.published_at, limits.published_since_year):
                    continue
                seen.add(document.url)
                produced += 1
                yield document
                if produced >= limits.max_documents_per_source:
                    return


def query_plan(terms: SearchTerms) -> list[tuple[str, str]]:
    """Фразы запроса: первая русская (массивы РФ и СНГ) и первая английская (мировой фонд)."""
    return [(term, "ru") for term in terms.ru[:TERMS_PER_LANGUAGE]] + [
        (term, "en") for term in terms.en[:TERMS_PER_LANGUAGE]
    ]


def request_body(term: str, limits: CollectionLimits) -> dict[str, Any]:
    """Тело `POST /search`: запрос на естественном языке, объём выдачи и нижняя граница даты публикации."""
    return {
        "qn": term,
        "limit": min(PAGE_SIZE, limits.max_documents_per_source),
        "filter": {"date_published": {"range": {"gte": f"{limits.published_since_year}0101"}}},
    }


def _hits(payload: Any) -> list[dict[str, Any]]:
    """Документы выдачи; структурно неверный ответ → PARSE_ERROR."""
    if not isinstance(payload, dict):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Роспатент: ответ не объект JSON")
    hits = payload.get("hits", [])
    if not isinstance(hits, list):
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, "Роспатент: поле hits не список")
    return [hit for hit in hits if isinstance(hit, dict)]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _to_raw_document(hit: dict[str, Any], term: str, language: str) -> RawDocument | None:
    """Документ выдачи → сырой документ; None без идентификатора или названия."""
    doc_id = _text(hit.get("id"))
    if not doc_id:
        return None
    biblio = _dict(hit.get("biblio"))
    snippet = _dict(hit.get("snippet"))
    common = _dict(hit.get("common"))
    snippet_language = _text(snippet.get("lang")).lower()
    text_language = snippet_language or language
    title = _title(biblio, text_language) or _text(snippet.get("title"))
    if not title:
        return None
    return RawDocument(
        url=f"{DOC_URL}{quote(doc_id, safe='')}",
        title=title,
        text=_text(snippet.get("description")),
        matched_term=term,
        published_at=parse_publication_date(common.get("publication_date")),
        language_code=text_language or None,
        raw_meta={
            "ipc": _ipc(common),
            "applicant": _names(biblio, text_language, snippet),
            "kind": _text(common.get("kind")),
            "publishing_office": _text(common.get("publishing_office")),
            "dataset": _text(hit.get("dataset")),
        },
    )


def _title(biblio: dict[str, Any], language: str) -> str:
    """Название на языке фрагмента описания, иначе на любом доступном (ru, en, остальные)."""
    order = [language, "ru", "en", *sorted(biblio)]
    for code in dict.fromkeys(code for code in order if code):
        title = _text(_dict(biblio.get(code)).get("title"))
        if title:
            return title
    return ""


def _names(biblio: dict[str, Any], language: str, snippet: dict[str, Any]) -> list[str]:
    """Заявители (не более трёх) на языке названия; иначе строка заявителя из фрагмента."""
    for code in dict.fromkeys(code for code in (language, "ru", "en") if code):
        applicants = _dict(biblio.get(code)).get("applicant")
        if isinstance(applicants, list):
            names = [_text(item.get("name")) for item in applicants if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                return names[:MAX_NAMES]
    fallback = _text(snippet.get("applicant"))
    return [fallback] if fallback else []


def _ipc(common: dict[str, Any]) -> list[str]:
    """Коды МПК документа (не более пяти)."""
    entries = _dict(common.get("classification")).get("ipc")
    if not isinstance(entries, list):
        return []
    codes = [_text(entry.get("fullname")) for entry in entries if isinstance(entry, dict)]
    return [code for code in codes if code][:MAX_IPC]


def parse_publication_date(value: Any) -> Any:
    """Дата публикации: `YYYY.MM.DD` (пример ответа поиска) или `YYYYMMDD` (формат документа); иначе None."""
    match = _DATE_RE.match(_text(value))
    if not match:
        return None
    year, month, day = match.groups()
    return parse_datetime(f"{year}-{month}-{day}")
