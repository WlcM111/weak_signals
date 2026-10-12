"""Идентичность публикаций по ссылке (этап 4.6, аудит P02): общая для collector и benchmark.

Одна работа arXiv доступна как abs, pdf, html, ar5iv и в версиях vN — это один идентификатор `arxiv:<id>`.
Ссылка doi.org — `doi:<doi>`. Для остальных ссылок: схема и `www.` отбрасываются, хост — в нижнем регистре,
завершающий `/` убирается, удаляются только известные параметры отслеживания (utm_*, fbclid, gclid…), а смысловые
параметры (`article?id=1`) сохраняются в отсортированном виде — разные статьи не сливаются в одну.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_TRACKING = re.compile(
    r"^(utm_[a-z0-9_]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref|ref_src|spm|cmpid|ncid|ocid|sr_share|"
    r"_hsenc|_hsmi|mkt_tok|guccounter|guce_referrer|guce_referrer_sig|cid|src)$",
    re.IGNORECASE,
)
_ARXIV_PATH = re.compile(
    r"^/(?:abs|pdf|html|format)/(?P<id>\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?:v\d+)?(?:\.pdf)?/?$", re.IGNORECASE
)
_AR5IV_PATH = re.compile(r"^/(?:html|abs)/(?P<id>\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?:v\d+)?/?$", re.IGNORECASE)
_ARXIV_HOSTS = frozenset({"arxiv.org", "export.arxiv.org"})
_AR5IV_HOSTS = frozenset({"ar5iv.org", "ar5iv.labs.arxiv.org"})
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})


def arxiv_id(url: str) -> str | None:
    """Идентификатор arXiv без версии для ссылок abs/pdf/html/ar5iv, иначе None."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host in _ARXIV_HOSTS:
        match = _ARXIV_PATH.match(parts.path)
    elif host in _AR5IV_HOSTS:
        match = _AR5IV_PATH.match(parts.path)
    else:
        return None
    return match.group("id").lower() if match else None


def doi_of(url: str) -> str | None:
    """DOI из ссылки doi.org / dx.doi.org в нижнем регистре, иначе None."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host in _DOI_HOSTS and parts.path.startswith("/10."):
        return parts.path[1:].rstrip("/").lower()
    return None


def canonical_url(url: str) -> str:
    """Ссылка без схемы, `www.`, фрагмента, параметров отслеживания и завершающего `/`."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return host + path + (("?" + urlencode(sorted(query))) if query else "")


def identity_key(url: str, doi: str | None = None) -> str:
    """Ключ идентичности публикации: `arxiv:<id>`, `doi:<doi>` или каноническая ссылка."""
    found = arxiv_id(url)
    if found:
        return f"arxiv:{found}"
    found_doi = (doi or "").strip().lower() or doi_of(url)
    if found_doi:
        return f"doi:{found_doi}"
    return canonical_url(url)


def identity_keys(url: str, doi: str | None = None) -> set[str]:
    """Все ключи публикации: идентификатор arXiv/DOI и каноническая ссылка (для сверки разных форм)."""
    keys = {canonical_url(url), identity_key(url, doi)}
    if doi:
        keys.add(f"doi:{doi.strip().lower()}")
    return {key for key in keys if key}


def without_tracking(url: str) -> str:
    """Исходная ссылка без фрагмента и известных параметров отслеживания; смысловые параметры (`?p=495626`,
    `?id=101`) и схема сохраняются (этап 5.0, аудит round2 F13)."""
    parts = urlsplit((url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return f"{parts.scheme}://{host}{port}{parts.path or '/'}" + (("?" + urlencode(query)) if query else "")
