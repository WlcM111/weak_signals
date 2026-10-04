"""Значения для колонок jsonb: PostgreSQL не принимает NaN и бесконечность, а json.dumps — даты и перечисления."""

from __future__ import annotations

import math
import numbers
from typing import Any


def json_safe(value: Any) -> Any:
    """Рекурсивно: NaN и ±inf → None, кортежи и множества → списки, числа numpy → числа, прочее → строка."""
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_safe(item) for item in value]
    return str(getattr(value, "value", value))
