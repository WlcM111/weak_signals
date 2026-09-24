"""Валидация входных данных RPC (§10.1 ТЗ, `CONTRACT_RULES.md`).

Модуль не зависит от gRPC: use cases вызывают его напрямую, транспорт переводит `ValidationError`
в INVALID_ARGUMENT и передаёт `error_code` в trailing metadata.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from collector.domain.errors import ValidationError
from collector.domain.rules import decode_page_token
from collector.domain.values import (
    MAX_TERMS_PER_LANGUAGE,
    MAX_TERM_LENGTH,
    MIN_TERM_LENGTH,
    CollectionLimits,
    CollectionMode,
    SearchTerms,
    SourceKey,
)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")
_MEANINGFUL_RE = re.compile(r"[\w]", re.UNICODE)

MAX_QUERY_LENGTH = 500
MIN_QUERY_LENGTH = 2
MAX_DOCUMENT_IDS = 200
MAX_ENCYCLOPEDIA_TITLES = 20
MAX_CHUNK_SIZE = 200
DEFAULT_CHUNK_SIZE = 50
MAX_CANCEL_REASON = 200
SUPPORTED_ENCYCLOPEDIA_LANGUAGES = frozenset({"ru", "en"})


def validate_uuid(value: str, field: str) -> str:
    """UUID по RFC 4122 (36 символов)."""
    if not _UUID_RE.match(value or ""):
        raise ValidationError(f"{field}: ожидается UUID (36 символов)", "INVALID_UUID")
    return value


def validate_idempotency_key(value: str) -> str:
    """Ключ идемпотентности: 8..128 символов `[A-Za-z0-9:_-]`."""
    if not _IDEMPOTENCY_RE.match(value or ""):
        raise ValidationError(
            "idempotency_key: 8..128 символов из набора [A-Za-z0-9:_-]", "INVALID_IDEMPOTENCY_KEY"
        )
    return value


def validate_query_text(value: str) -> str:
    """Текст запроса: 2..500 символов после trim, не только знаки препинания."""
    text = (value or "").strip()
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH or _MEANINGFUL_RE.search(text) is None:
        raise ValidationError("query_text: 2..500 значащих символов", "INVALID_QUERY")
    return text


def validate_terms(ru: Sequence[str], en: Sequence[str]) -> SearchTerms:
    """Поисковые фразы: ru+en ≥ 1, каждая 2..120 символов, ≤ 8 в списке, без дублей."""
    cleaned: dict[str, tuple[str, ...]] = {}
    for name, values in (("ru", ru), ("en", en)):
        items = tuple((value or "").strip() for value in values)
        if len(items) > MAX_TERMS_PER_LANGUAGE:
            raise ValidationError(f"terms.{name}: не более {MAX_TERMS_PER_LANGUAGE} фраз", "INVALID_TERMS")
        if any(not MIN_TERM_LENGTH <= len(item) <= MAX_TERM_LENGTH for item in items):
            raise ValidationError(f"terms.{name}: длина фразы 2..120 символов", "INVALID_TERMS")
        if len({item.lower() for item in items}) != len(items):
            raise ValidationError(f"terms.{name}: дублирующиеся фразы", "INVALID_TERMS")
        cleaned[name] = items
    if not cleaned["ru"] and not cleaned["en"]:
        raise ValidationError("terms: требуется хотя бы одна фраза", "INVALID_TERMS")
    return SearchTerms(ru=cleaned["ru"], en=cleaned["en"])


def validate_limits(
    mode: CollectionMode,
    max_documents_per_source: int,
    max_total_documents: int,
    time_budget_seconds: int,
    published_since_year: int,
) -> CollectionLimits:
    """Лимиты: 0 = значение по умолчанию режима; иначе диапазоны из proto/DDL."""
    raw = {
        "max_documents_per_source": max_documents_per_source,
        "max_total_documents": max_total_documents,
        "time_budget_seconds": time_budget_seconds,
        "published_since_year": published_since_year,
    }
    for name, value in raw.items():
        low, high = CollectionLimits.RANGES[name]
        if value != 0 and not low <= value <= high:
            raise ValidationError(f"limits.{name}: допустимо 0 или {low}..{high}", "INVALID_LIMITS")
    return CollectionLimits.from_request(mode, **raw)


def validate_mode(mode: CollectionMode | None) -> CollectionMode:
    """Режим сбора обязателен и не может быть UNSPECIFIED."""
    if mode is None:
        raise ValidationError("mode: недопустимое значение перечисления", "INVALID_ENUM")
    return mode


def validate_sources(sources: Sequence[SourceKey | None]) -> tuple[SourceKey, ...]:
    """Список источников: пустой допустим (= все включённые); UNSPECIFIED недопустим; без дублей."""
    if any(source is None for source in sources):
        raise ValidationError("sources: недопустимое значение перечисления", "INVALID_ENUM")
    keys = tuple(source for source in sources if source is not None)
    if len(set(keys)) != len(keys):
        raise ValidationError("sources: дублирующиеся значения", "INVALID_ENUM")
    return keys


def validate_chunk_size(value: int) -> int:
    """Размер чанка стрима: 1..200, 0 = 50."""
    if value == 0:
        return DEFAULT_CHUNK_SIZE
    if not 1 <= value <= MAX_CHUNK_SIZE:
        raise ValidationError("chunk_size: 1..200 (0 = 50)", "INVALID_LIMITS")
    return value


def validate_page_token(token: str) -> tuple[int, str] | None:
    """Keyset-токен страницы; пустая строка = с начала, подделка = INVALID_PAGE_TOKEN."""
    if not token:
        return None
    try:
        return decode_page_token(token)
    except ValueError as exc:
        raise ValidationError("page_token: непрозрачный токен повреждён", "INVALID_PAGE_TOKEN") from exc


def validate_document_ids(document_ids: Sequence[str]) -> tuple[str, ...]:
    """Идентификаторы документов: 1..200 корректных UUID."""
    if not 1 <= len(document_ids) <= MAX_DOCUMENT_IDS:
        raise ValidationError("document_ids: от 1 до 200 идентификаторов", "INVALID_ARGUMENT")
    return tuple(validate_uuid(document_id, "document_ids") for document_id in document_ids)


def validate_encyclopedia_request(titles: Sequence[str], language_code: str) -> tuple[tuple[str, ...], str]:
    """Названия: 1..20 непустых; язык: `ru` или `en`."""
    cleaned = tuple((title or "").strip() for title in titles)
    if not 1 <= len(cleaned) <= MAX_ENCYCLOPEDIA_TITLES:
        raise ValidationError("titles: от 1 до 20 названий", "INVALID_ARGUMENT")
    if any(not title for title in cleaned):
        raise ValidationError("titles: пустое название", "INVALID_ARGUMENT")
    language = (language_code or "").strip().lower()
    if language not in SUPPORTED_ENCYCLOPEDIA_LANGUAGES:
        raise ValidationError("language_code: допустимы 'ru' и 'en'", "INVALID_ENUM")
    return cleaned, language


def validate_cancel_reason(reason: str) -> str:
    """Причина отмены: ≤ 200 символов."""
    text = (reason or "").strip()
    if len(text) > MAX_CANCEL_REASON:
        raise ValidationError("reason: не более 200 символов", "INVALID_ARGUMENT")
    return text
