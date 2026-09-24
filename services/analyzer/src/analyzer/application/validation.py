"""Валидация входных данных RPC analyzer (§10.1 ТЗ, `CONTRACT_RULES.md`).

Модуль не зависит от gRPC: правила проверяются напрямую, транспорт лишь переводит `ValidationError`
в INVALID_ARGUMENT и кладёт `error_code` в trailing metadata.
"""

from __future__ import annotations

import base64
import binascii
import re

from analyzer.domain.errors import ValidationError
from analyzer.domain.values import AnalysisParams

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")
_MEANINGFUL_RE = re.compile(r"[\w]", re.UNICODE)

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 500
MIN_TITLE_LENGTH = 2
MAX_TITLE_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 4000
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
MAX_CANCEL_REASON = 200
MAX_THRESHOLD_EXCLUSIVE = 1.0


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
    """Текст запроса: 2..500 значащих символов."""
    text = (value or "").strip()
    if not MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH or _MEANINGFUL_RE.search(text) is None:
        raise ValidationError("query_text: 2..500 значащих символов", "INVALID_QUERY")
    return text


def validate_params(
    top_n: int, max_candidates: int, weak_signal_threshold: float, min_evidence_documents: int
) -> tuple[int, int, float, int]:
    """Диапазоны параметров анализа; 0 означает значение по умолчанию."""
    for name, value in (
        ("top_n", top_n),
        ("max_candidates", max_candidates),
        ("min_evidence_documents", min_evidence_documents),
    ):
        low, high = AnalysisParams.RANGES[name]
        if value != 0 and not low <= value <= high:
            raise ValidationError(f"params.{name}: допустимо 0 или {low}..{high}", "INVALID_PARAMS")
    if weak_signal_threshold != 0.0 and not 0.0 < weak_signal_threshold < MAX_THRESHOLD_EXCLUSIVE:
        raise ValidationError(
            "params.weak_signal_threshold: допустимо 0 или значение строго между 0 и 1", "INVALID_PARAMS"
        )
    return top_n, max_candidates, float(weak_signal_threshold), min_evidence_documents


def validate_page_size(value: int) -> int:
    """Размер страницы: 1..200, 0 = 50."""
    if value == 0:
        return DEFAULT_PAGE_SIZE
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise ValidationError("page.page_size: 1..200 (0 = 50)", "INVALID_LIMITS")
    return value


def encode_page_token(group: int, order_value: float, cluster_index: int) -> str:
    """Непрозрачный keyset-токен страницы кандидатов."""
    return base64.urlsafe_b64encode(f"{group}:{order_value!r}:{cluster_index}".encode()).decode("ascii")


def validate_page_token(token: str) -> tuple[int, float, int] | None:
    """Разбор keyset-токена; пустая строка = с начала, подделка = INVALID_PAGE_TOKEN."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        group, order_value, cluster_index = raw.split(":")
        return int(group), float(order_value), int(cluster_index)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("page.page_token: непрозрачный токен повреждён", "INVALID_PAGE_TOKEN") from exc


def validate_score_text(title: str, description: str) -> tuple[str, str]:
    """Заголовок 2..300 значащих символов, описание ≤ 4000."""
    cleaned_title = (title or "").strip()
    if (
        not MIN_TITLE_LENGTH <= len(cleaned_title) <= MAX_TITLE_LENGTH
        or _MEANINGFUL_RE.search(cleaned_title) is None
    ):
        raise ValidationError("title: 2..300 значащих символов", "INVALID_ARGUMENT")
    cleaned_description = (description or "").strip()
    if len(cleaned_description) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError("description: не более 4000 символов", "INVALID_ARGUMENT")
    return cleaned_title, cleaned_description


def validate_cancel_reason(reason: str) -> str:
    """Причина отмены: ≤ 200 символов."""
    text = (reason or "").strip()
    if len(text) > MAX_CANCEL_REASON:
        raise ValidationError("reason: не более 200 символов", "INVALID_ARGUMENT")
    return text
