"""Разбор и валидация JSON-ответа LLM (§7 HANDOFF).

Модель иногда оборачивает JSON в markdown или добавляет пояснения; «мягкий ремонт» вырезает
объект по внешним скобкам. Валидация выполняется нормативным минимальным валидатором
`tools/minischema.py` — тем же, которым проверяется весь комплект.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parents[5] / "tools"


class OutputRejected(ValueError):
    """Ответ модели не разобран или не соответствует схеме; текст ответа наружу не выносится."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def repair_json(raw: str) -> str:
    """Вырезает JSON-объект из ответа: снимает markdown-ограждение и текст вокруг."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.lstrip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text.strip()
    return text[start : end + 1].strip()


def parse(raw: str) -> dict[str, Any]:
    """Разбирает ответ модели в словарь; иначе — `OutputRejected`."""
    try:
        payload = json.loads(repair_json(raw))
    except json.JSONDecodeError as error:
        raise OutputRejected(f"ответ не является корректным JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise OutputRejected("ответ должен быть объектом JSON")
    return payload


def validate(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Проверяет объект по JSON-схеме; возвращает список нарушений."""
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    import minischema  # noqa: PLC0415 - нормативный валидатор комплекта

    return list(minischema.validate_root(payload, schema))


def parse_and_validate(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Разбор и валидация одним шагом; нарушения перечисляются в сообщении."""
    payload = parse(raw)
    problems = validate(payload, schema)
    if problems:
        raise OutputRejected("ответ не соответствует схеме: " + "; ".join(problems[:5]))
    return payload
