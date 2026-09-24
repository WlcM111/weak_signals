"""Валидация HTTP-входа и канонизация тела для идемпотентности (§15 HANDOFF)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import datetime
from typing import Any

from orchestrator.domain.errors import ValidationError
from orchestrator.domain.values import (
    MAX_QUERY_LENGTH,
    MAX_TOP_N,
    MIN_QUERY_LENGTH,
    MIN_TOP_N,
    JobStatus,
)

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MEANINGFUL_RE = re.compile(r"[\w]", re.UNICODE)
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_TITLE_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 4000


def validate_query_text(value: str) -> str:
    """Текст запроса: 2..500 значащих символов."""
    text = " ".join((value or "").split())
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH or _MEANINGFUL_RE.search(text) is None:
        raise ValidationError("query_text: 2..500 значащих символов", "VALIDATION_ERROR")
    return text


def _as_int(value: Any, field: str) -> int:
    """Целое из параметра запроса; нечисловое значение — ошибка валидации (HTTP 400), а не HTTP 500."""
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{field}: ожидается целое число", "VALIDATION_ERROR") from error


def validate_top_n(value: int | str | None) -> int:
    """Размер выдачи: 1..50, по умолчанию 15."""
    if value in (None, "", 0, "0"):
        return 15
    number = _as_int(value, "top_n")
    if not MIN_TOP_N <= number <= MAX_TOP_N:
        raise ValidationError("top_n: 1..50", "VALIDATION_ERROR")
    return number


def validate_idempotency_key(value: str | None) -> str:
    """Заголовок `Idempotency-Key` обязателен для POST (§22 HANDOFF)."""
    if not value:
        raise ValidationError("заголовок Idempotency-Key обязателен", "VALIDATION_ERROR")
    if not _IDEMPOTENCY_RE.match(value):
        raise ValidationError(
            "Idempotency-Key: 8..128 символов из набора [A-Za-z0-9:_-]", "VALIDATION_ERROR"
        )
    return value


def validate_uuid(value: str, field: str) -> str:
    """UUID пути запроса."""
    if not _UUID_RE.match(value or ""):
        raise ValidationError(f"{field}: ожидается UUID", "VALIDATION_ERROR")
    return value


def validate_page_size(value: int | str | None) -> int:
    """Размер страницы списка заданий: 1..100, по умолчанию 20."""
    if value in (None, "", 0, "0"):
        return DEFAULT_PAGE_SIZE
    number = _as_int(value, "limit")
    if not 1 <= number <= MAX_PAGE_SIZE:
        raise ValidationError("limit: 1..100", "VALIDATION_ERROR")
    return number


def validate_status_filter(value: str | None) -> JobStatus | None:
    """Фильтр списка по статусу."""
    if not value:
        return None
    try:
        return JobStatus(value)
    except ValueError as error:
        raise ValidationError(f"status: недопустимое значение {value}", "VALIDATION_ERROR") from error


def validate_score_request(title: str, description: str) -> tuple[str, str]:
    """Заголовок 2..300 значащих символов, описание ≤ 4000."""
    cleaned_title = " ".join((title or "").split())
    if not 2 <= len(cleaned_title) <= MAX_TITLE_LENGTH or _MEANINGFUL_RE.search(cleaned_title) is None:
        raise ValidationError("title: 2..300 значащих символов", "VALIDATION_ERROR")
    cleaned_description = (description or "").strip()
    if len(cleaned_description) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError("description: не более 4000 символов", "VALIDATION_ERROR")
    return cleaned_title, cleaned_description


def canonical_request_hash(payload: dict[str, Any]) -> str:
    """sha256 канонизированного JSON тела: ключи отсортированы, разделители без пробелов."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_cursor(created_at: datetime, job_id: str) -> str:
    """Непрозрачный keyset-курсор списка заданий."""
    raw = f"{created_at.isoformat()}|{job_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """Разбор курсора; подделка — ошибка валидации."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        moment, job_id = raw.split("|", 1)
        return datetime.fromisoformat(moment), validate_uuid(job_id, "cursor")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ValidationError("cursor: непрозрачный токен повреждён", "VALIDATION_ERROR") from error


def check_api_key(provided: str | None, expected: str) -> bool:
    """Сравнение API-ключа за постоянное время (§17 HANDOFF).

    Сравниваются байты UTF-8: `hmac.compare_digest` для строк поднимает TypeError на не-ASCII
    символах, и любой запрос с таким заголовком превращался в HTTP 500 вместо 401.
    """
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def hash_client_ip(ip: str | None, salt: str) -> str | None:
    """`sha256(ip + соль)` для rate limit и аудита; исходный адрес не сохраняется."""
    if not ip:
        return None
    return hashlib.sha256(f"{ip}{salt}".encode()).hexdigest()
