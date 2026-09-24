"""Валидация входных данных RPC insight (§4, §14 HANDOFF)."""

from __future__ import annotations

import re

from insight.domain.errors import ValidationError
from insight.domain.values import (
    MAX_EVIDENCE,
    MAX_EVIDENCE_TEXT,
    MAX_FEATURES,
    MAX_IDEMPOTENCY_KEY,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
)

_MEANINGFUL_RE = re.compile(r"[\w]", re.UNICODE)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MIN_TOKENS = 256
MAX_TOKENS = 2048


def validate_query_text(value: str) -> str:
    """Текст запроса: 2..500 значащих символов."""
    text = " ".join((value or "").split())
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH or _MEANINGFUL_RE.search(text) is None:
        raise ValidationError("query_text: 2..500 значащих символов", "INVALID_ARGUMENT")
    return text


def validate_idempotency_key(value: str) -> str:
    """Ключ идемпотентности: непустой, не длиннее 160 символов."""
    key = (value or "").strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY:
        raise ValidationError(
            f"idempotency_key: 1..{MAX_IDEMPOTENCY_KEY} символов", "INVALID_ARGUMENT"
        )
    return key


def validate_uuid(value: str, field: str) -> str:
    """UUID сущности другого сервиса."""
    if not _UUID_RE.match(value or ""):
        raise ValidationError(f"{field}: ожидается UUID", "INVALID_ARGUMENT")
    return value


def validate_evidence_count(count: int) -> int:
    """Доказательств должно быть от 1 до 8 (§4 контракта)."""
    if not 1 <= count <= MAX_EVIDENCE:
        raise ValidationError(
            f"evidence: требуется от 1 до {MAX_EVIDENCE} документов", "INVALID_ARGUMENT"
        )
    return count


def validate_features_count(count: int) -> int:
    """Признаков не больше восьми."""
    if count > MAX_FEATURES:
        raise ValidationError(
            f"candidate.top_features: не более {MAX_FEATURES} признаков", "INVALID_ARGUMENT"
        )
    return count


def truncate_evidence_text(text: str) -> str:
    """Текст доказательства усекается до 2000 символов (страховка на стороне сервера)."""
    return " ".join((text or "").split())[:MAX_EVIDENCE_TEXT]


def validate_max_output_tokens(value: int) -> int:
    """Лимит вывода: 0 (по умолчанию) либо 256..2048."""
    if value == 0:
        return 0
    if not MIN_TOKENS <= value <= MAX_TOKENS:
        raise ValidationError(
            f"max_output_tokens: 0 или {MIN_TOKENS}..{MAX_TOKENS}", "INVALID_ARGUMENT"
        )
    return value
