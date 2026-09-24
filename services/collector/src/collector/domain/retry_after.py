"""Разбор заголовка Retry-After в обеих формах RFC 9110: delta-seconds и HTTP-date."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Секунды ожидания (≥ 0) или None, если заголовка нет или он не разбирается."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(int(text))
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return max(0.0, number)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())
