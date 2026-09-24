"""Преобразование сырого документа адаптера в нормализованный черновик (§9.2 ТЗ).

Отдельный модуль, чтобы избежать цикла импортов `rules ← entities` и оставить правила чистыми.
"""

from __future__ import annotations

from datetime import datetime

from collector.domain.classify import ClassificationConfig, classify
from collector.domain.entities import DocumentDraft, RawDocument
from collector.domain.rules import (
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_URL_LENGTH,
    canonical_url,
    clean_whitespace,
    content_hash,
    detect_language,
    normalize_doi,
    normalize_language_code,
    origin_domain,
    sanitize_raw_meta,
    strip_html,
    truncate,
    url_hash,
)
from collector.domain.values import SourceKey


def build_document_draft(
    raw: RawDocument,
    *,
    source_key: SourceKey,
    config: ClassificationConfig,
    raw_meta_keys: frozenset[str],
    fetched_at: datetime,
) -> DocumentDraft:
    """Нормализует, классифицирует и хеширует документ; ValueError при непригодном URL или пустом заголовке."""
    if len(raw.url) > MAX_URL_LENGTH:
        raise ValueError("URL длиннее 2048 символов")
    canonical = canonical_url(raw.url)
    domain = origin_domain(canonical)
    title = truncate(clean_whitespace(strip_html(raw.title)), MAX_TITLE_LENGTH)
    if not title:
        raise ValueError("документ без заголовка")
    text = truncate(clean_whitespace(strip_html(raw.text)), MAX_TEXT_LENGTH)
    language = normalize_language_code(raw.language_code) or detect_language(f"{title} {text}")
    source_type, trust_level = classify(
        config, source_key, domain, title, has_published_at=raw.published_at is not None
    )
    return DocumentDraft(
        url=raw.url.strip(),
        canonical_url=canonical,
        url_hash=url_hash(canonical),
        origin_domain=domain,
        title=title,
        text=text,
        content_hash=content_hash(title, text),
        language_code=language,
        source_key=source_key,
        source_type=source_type,
        trust_level=trust_level,
        fetched_at=fetched_at,
        matched_term=truncate(raw.matched_term, 120),
        published_at=raw.published_at,
        doi=normalize_doi(raw.doi),
        citation_count=raw.citation_count if raw.citation_count is None or raw.citation_count >= 0 else None,
        engagement_count=(
            raw.engagement_count if raw.engagement_count is None or raw.engagement_count >= 0 else None
        ),
        raw_meta=sanitize_raw_meta(raw.raw_meta, raw_meta_keys),
    )
